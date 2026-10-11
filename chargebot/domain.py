"""Pure charger/queue state machine.

Nothing in this module does I/O, reads the clock, or touches threads. Every
operation takes the current state plus ``now`` and mutates the state in place,
returning a :class:`Result` whose ``effects`` describe what the outside world
should do (send a DM, update a message, send a calendar invite, ...).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .config import Config

# ---------------------------------------------------------------------------
# Effects
# ---------------------------------------------------------------------------


@dataclass
class Notify:
    """DM a user. ``track`` names the object whose buttons this message carries."""

    user_id: str
    kind: str
    data: Dict[str, Any] = field(default_factory=dict)
    track: Optional[str] = None


@dataclass
class CloseMessages:
    """Replace the buttons on previously sent DMs with a short outcome line."""

    refs: List[Dict[str, str]]
    reason: str
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SendInvite:
    user_id: str
    uid: str
    start: float
    end: float
    sequence: int


@dataclass
class CancelInvite:
    user_id: str
    uid: str
    start: float
    end: float
    sequence: int


@dataclass
class LogSession:
    record: Dict[str, Any]


@dataclass
class StatusChanged:
    pass


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class Session:
    id: str
    user_id: str
    charger_id: str
    joined_at: float
    offered_at: float
    grace_ends_at: float
    confirm_deadline: float
    confirmed_at: Optional[float] = None
    charge_start: Optional[float] = None
    ends_at: Optional[float] = None
    alerts_sent: int = 0
    warned: bool = False
    extensions: int = 0
    invite_uid: Optional[str] = None
    invite_seq: int = 0
    messages: List[Dict[str, str]] = field(default_factory=list)

    @property
    def confirmed(self) -> bool:
        return self.confirmed_at is not None


@dataclass
class Unplug:
    """A user whose session ended and who should move their car."""

    user_id: str
    since: float
    last_nudged_at: Optional[float] = None
    messages: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class Charger:
    id: str
    name: str
    session: Optional[Session] = None
    out_of_order: Optional[str] = None
    unplug: Optional[Unplug] = None
    reclaim: Optional[Session] = None


@dataclass
class QueueEntry:
    user_id: str
    joined_at: float
    heads_up_sent: bool = False


@dataclass
class State:
    chargers: List[Charger] = field(default_factory=list)
    queue: List[QueueEntry] = field(default_factory=list)
    prefs: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    meta: Dict[str, Any] = field(default_factory=dict)
    seq: int = 0
    version: int = 0

    # -- serialization -----------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "State":
        def session(d: Optional[Dict[str, Any]]) -> Optional[Session]:
            return Session(**d) if d else None

        chargers = []
        for c in data.get("chargers", []):
            unplug = c.get("unplug")
            chargers.append(Charger(
                id=c["id"],
                name=c["name"],
                session=session(c.get("session")),
                out_of_order=c.get("out_of_order"),
                unplug=Unplug(**unplug) if unplug else None,
                reclaim=session(c.get("reclaim")),
            ))
        return cls(
            chargers=chargers,
            queue=[QueueEntry(**q) for q in data.get("queue", [])],
            prefs=dict(data.get("prefs", {})),
            meta=dict(data.get("meta", {})),
            seq=int(data.get("seq", 0)),
            version=int(data.get("version", 0)),
        )

    # -- lookups -----------------------------------------------------------

    def charger(self, charger_id: str) -> Optional[Charger]:
        return next((c for c in self.chargers if c.id == charger_id), None)

    def session_of(self, user_id: str) -> Tuple[Optional[Charger], Optional[Session]]:
        for c in self.chargers:
            if c.session and c.session.user_id == user_id:
                return c, c.session
        return None, None

    def queue_position(self, user_id: str) -> Optional[int]:
        for i, entry in enumerate(self.queue):
            if entry.user_id == user_id:
                return i
        return None

    def free_chargers(self) -> List[Charger]:
        return [c for c in self.chargers if c.session is None and not c.out_of_order]

    def wants_email(self, user_id: str) -> bool:
        return bool(self.prefs.get(user_id, {}).get("email_reminder", True))


def new_state(cfg: Config) -> State:
    state = State()
    ensure_chargers(state, cfg)
    return state


def ensure_chargers(state: State, cfg: Config) -> None:
    """Make the charger list match the configuration (ids are c1, c2, ...)."""
    wanted = [(f"c{i + 1}", name) for i, name in enumerate(cfg.chargers)]
    by_id = {c.id: c for c in state.chargers}
    result = []
    for cid, name in wanted:
        charger = by_id.pop(cid, None) or Charger(id=cid, name=name)
        charger.name = name
        result.append(charger)
    # Keep removed chargers only while someone is still using them.
    result.extend(c for c in by_id.values() if c.session is not None)
    state.chargers = result


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class Result:
    code: str
    ok: bool = True
    data: Dict[str, Any] = field(default_factory=dict)
    effects: List[Any] = field(default_factory=list)
    changed: bool = False


def _fail(code: str, **data: Any) -> Result:
    return Result(code=code, ok=False, data=data)


# ---------------------------------------------------------------------------
# Estimates
# ---------------------------------------------------------------------------


def charger_free_at(cfg: Config, charger: Charger, now: float) -> Optional[float]:
    """Best guess of when a charger can be offered to someone new."""
    if charger.out_of_order:
        return None
    s = charger.session
    if s is None:
        return now
    if s.ends_at is not None:
        return max(now, s.ends_at)
    return max(now, s.grace_ends_at + cfg.charge_s)


def queue_etas(cfg: Config, state: State, now: float) -> List[Optional[float]]:
    """Estimated offer time for each queue entry, in queue order."""
    free = [t for t in (charger_free_at(cfg, c, now) for c in state.chargers) if t is not None]
    if not free:
        return [None] * len(state.queue)
    etas: List[Optional[float]] = []
    for _ in state.queue:
        i = min(range(len(free)), key=lambda k: free[k])
        etas.append(free[i])
        free[i] = free[i] + cfg.grace_s + cfg.charge_s
    return etas


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


class Rules:
    """All state transitions. Each public method returns a :class:`Result`."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    # -- helpers -----------------------------------------------------------

    def _done(self, result: Result) -> Result:
        if result.effects or result.changed:
            result.changed = True
            result.effects.append(StatusChanged())
        return result

    def _close(self, result: Result, holder: Any, reason: str, **data: Any) -> None:
        if holder is not None and holder.messages:
            result.effects.append(CloseMessages(refs=list(holder.messages), reason=reason, data=data))
            holder.messages = []

    def _offer(self, state: State, charger: Charger, user_id: str, joined_at: float,
               now: float, result: Result) -> Session:
        if charger.reclaim is not None:
            self._close(result, charger.reclaim, "reclaim_expired")
            charger.reclaim = None
        state.seq += 1
        session = Session(
            id=f"s{state.seq}",
            user_id=user_id,
            charger_id=charger.id,
            joined_at=joined_at,
            offered_at=now,
            grace_ends_at=now + self.cfg.grace_s,
            confirm_deadline=now + self.cfg.confirm_s,
        )
        charger.session = session
        result.effects.append(Notify(user_id, "offered", {
            "charger_id": charger.id,
            "grace_ends_at": session.grace_ends_at,
            "confirm_deadline": session.confirm_deadline,
            "previous_user_id": charger.unplug.user_id if charger.unplug else None,
        }, track=session.id))
        result.changed = True
        return session

    def _promote(self, state: State, now: float, result: Result) -> None:
        for charger in state.free_chargers():
            if not state.queue:
                return
            entry = state.queue.pop(0)
            self._offer(state, charger, entry.user_id, entry.joined_at, now, result)

    def _log(self, result: Result, session: Session, now: float, outcome: str) -> None:
        result.effects.append(LogSession({
            "id": session.id,
            "user_id": session.user_id,
            "charger_id": session.charger_id,
            "joined_at": session.joined_at,
            "offered_at": session.offered_at,
            "confirmed_at": session.confirmed_at,
            "charge_start": session.charge_start,
            "ended_at": now,
            "extensions": session.extensions,
            "outcome": outcome,
        }))

    def _end(self, state: State, charger: Charger, now: float, outcome: str,
             result: Result, close_reason: Optional[str] = None) -> Session:
        session = charger.session
        assert session is not None
        self._log(result, session, now, outcome)
        if session.invite_uid and outcome != "completed":
            ends = session.ends_at or now
            result.effects.append(CancelInvite(
                session.user_id, session.invite_uid, ends, ends + self.cfg.grace_s, session.invite_seq + 1))
        self._close(result, session, close_reason or outcome, at=now)
        charger.session = None
        result.changed = True
        return session

    def _start_charging(self, state: State, charger: Charger, session: Session,
                        now: float, result: Result) -> None:
        session.confirmed_at = now
        if session.charge_start is None:
            session.charge_start = now
            session.ends_at = now + self.cfg.charge_s
        session.alerts_sent = len(self.cfg.confirm_alerts_s)
        if charger.unplug is not None:
            self._close(result, charger.unplug, "handed_over")
            charger.unplug = None
        if state.wants_email(session.user_id) and session.ends_at is not None:
            session.invite_uid = f"chargebot-{session.id}-{int(session.offered_at)}@chargebot"
            session.invite_seq = 0
            result.effects.append(SendInvite(
                session.user_id, session.invite_uid, session.ends_at,
                session.ends_at + self.cfg.grace_s, session.invite_seq))
        result.effects.append(Notify(session.user_id, "charging", {
            "charger_id": charger.id,
            "ends_at": session.ends_at,
        }, track=session.id))
        result.changed = True

    def _eta_for(self, state: State, user_id: str, now: float) -> Optional[float]:
        pos = state.queue_position(user_id)
        if pos is None:
            return None
        return queue_etas(self.cfg, state, now)[pos]

    # -- user actions --------------------------------------------------------

    def join(self, state: State, user_id: str, now: float) -> Result:
        charger, session = state.session_of(user_id)
        if session is not None:
            return _fail("already_active", charger_id=charger.id, confirmed=session.confirmed)
        pos = state.queue_position(user_id)
        if pos is not None:
            return _fail("already_queued", position=pos + 1, eta=self._eta_for(state, user_id, now))

        result = Result(code="offered")
        free = state.free_chargers()
        if free and not state.queue:
            charger = next((c for c in free if c.reclaim is None), free[0])
            self._offer(state, charger, user_id, now, now, result)
            result.data = {"charger_id": charger.id, "confirm_deadline": charger.session.confirm_deadline}
            return self._done(result)

        entry = QueueEntry(user_id=user_id, joined_at=now)
        state.queue.append(entry)
        eta = self._eta_for(state, user_id, now)
        if eta is not None and eta - now <= self.cfg.next_up_heads_up_s:
            entry.heads_up_sent = True
        result.code = "queued"
        result.data = {"position": len(state.queue), "eta": eta}
        result.changed = True
        return self._done(result)

    def confirm(self, state: State, user_id: str, now: float) -> Result:
        charger, session = state.session_of(user_id)
        if session is None:
            return _fail("not_offered")
        if session.confirmed:
            return _fail("already_confirmed", ends_at=session.ends_at)
        result = Result(code="confirmed")
        self._close(result, session, "confirmed", at=now)
        self._start_charging(state, charger, session, now, result)
        result.data = {"charger_id": charger.id, "ends_at": session.ends_at}
        return self._done(result)

    def skip(self, state: State, user_id: str, now: float) -> Result:
        charger, session = state.session_of(user_id)
        if session is None:
            return _fail("not_offered")
        if session.confirmed:
            return _fail("already_confirmed", ends_at=session.ends_at)
        result = Result(code="skipped")
        self._end(state, charger, now, "skipped", result)
        self._promote(state, now, result)
        return self._done(result)

    def done(self, state: State, user_id: str, now: float) -> Result:
        """'I'm done' — ends a session early, or confirms the car was unplugged."""
        charger, session = state.session_of(user_id)
        if session is not None:
            if not session.confirmed:
                return self.skip(state, user_id, now)
            result = Result(code="done", data={"charger_id": charger.id})
            self._end(state, charger, now, "ended_early", result, close_reason="done")
            self._promote(state, now, result)
            return self._done(result)
        unplugged = self.unplugged(state, user_id, now)
        if unplugged.ok:
            return unplugged
        return _fail("nothing_to_end", queued=state.queue_position(user_id) is not None)

    def unplugged(self, state: State, user_id: str, now: float) -> Result:
        for charger in state.chargers:
            if charger.unplug and charger.unplug.user_id == user_id:
                result = Result(code="unplugged", changed=True)
                self._close(result, charger.unplug, "unplugged", at=now)
                charger.unplug = None
                return self._done(result)
        return _fail("nothing_to_unplug")

    def leave(self, state: State, user_id: str, now: float) -> Result:
        pos = state.queue_position(user_id)
        if pos is None:
            _, session = state.session_of(user_id)
            return _fail("not_queued", has_session=session is not None)
        state.queue.pop(pos)
        return self._done(Result(code="left", changed=True))

    def extend(self, state: State, user_id: str, now: float) -> Result:
        charger, session = state.session_of(user_id)
        if session is None or not session.confirmed:
            return _fail("not_charging")
        if state.queue:
            return _fail("queue_waiting", waiting=len(state.queue))
        if session.extensions >= self.cfg.max_extensions:
            return _fail("max_extensions", ends_at=session.ends_at)
        result = Result(code="extended")
        session.ends_at = (session.ends_at or now) + self.cfg.extend_s
        session.extensions += 1
        session.warned = False
        self._close(result, session, "extended", ends_at=session.ends_at)
        if session.invite_uid:
            session.invite_seq += 1
            result.effects.append(SendInvite(
                session.user_id, session.invite_uid, session.ends_at,
                session.ends_at + self.cfg.grace_s, session.invite_seq))
        result.data = {"ends_at": session.ends_at}
        result.changed = True
        return self._done(result)

    def nudge(self, state: State, user_id: str, now: float) -> Result:
        for charger in state.chargers:
            s = charger.session
            if s and s.user_id == user_id and charger.unplug:
                unplug = charger.unplug
                if unplug.last_nudged_at and now - unplug.last_nudged_at < self.cfg.nudge_cooldown_s:
                    wait = self.cfg.nudge_cooldown_s - (now - unplug.last_nudged_at)
                    return _fail("nudge_too_soon", wait_s=int(wait))
                unplug.last_nudged_at = now
                result = Result(code="nudged", data={"target_user_id": unplug.user_id})
                result.effects.append(Notify(unplug.user_id, "nudge", {
                    "from_user_id": user_id, "charger_id": charger.id}, track=f"unplug:{charger.id}"))
                result.changed = True
                return self._done(result)
        return _fail("nothing_to_nudge")

    def reclaim(self, state: State, user_id: str, now: float) -> Result:
        for charger in state.chargers:
            lost = charger.reclaim
            if lost and lost.user_id == user_id and charger.session is None and not charger.out_of_order:
                result = Result(code="reclaimed")
                self._close(result, lost, "reclaimed", at=now)
                charger.reclaim = None
                charger.session = lost
                pos = state.queue_position(user_id)
                if pos is not None:
                    state.queue.pop(pos)
                self._start_charging(state, charger, lost, now, result)
                result.data = {"charger_id": charger.id, "ends_at": lost.ends_at}
                return self._done(result)
        return _fail("reclaim_expired")

    def set_pref(self, state: State, user_id: str, key: str, value: Any) -> Result:
        state.prefs.setdefault(user_id, {})[key] = value
        return Result(code="pref_saved", changed=True, data={key: value})

    # -- admin actions -------------------------------------------------------

    def admin_end(self, state: State, charger_id: str, now: float, by: str) -> Result:
        charger = state.charger(charger_id)
        if charger is None or charger.session is None:
            return _fail("no_session")
        result = Result(code="ended", data={"user_id": charger.session.user_id})
        session = self._end(state, charger, now, "kicked", result)
        result.effects.append(Notify(session.user_id, "kicked", {"by": by, "charger_id": charger.id}))
        self._promote(state, now, result)
        return self._done(result)

    def admin_remove(self, state: State, user_id: str, now: float, by: str) -> Result:
        pos = state.queue_position(user_id)
        if pos is None:
            return _fail("not_queued")
        state.queue.pop(pos)
        result = Result(code="removed", data={"user_id": user_id}, changed=True)
        result.effects.append(Notify(user_id, "removed", {"by": by}))
        return self._done(result)

    def admin_move_up(self, state: State, user_id: str, now: float) -> Result:
        pos = state.queue_position(user_id)
        if pos is None:
            return _fail("not_queued")
        if pos == 0:
            return _fail("already_first")
        q = state.queue
        q[pos - 1], q[pos] = q[pos], q[pos - 1]
        return self._done(Result(code="moved", changed=True))

    def set_out_of_order(self, state: State, charger_id: str, reason: Optional[str], now: float) -> Result:
        charger = state.charger(charger_id)
        if charger is None:
            return _fail("no_charger")
        charger.out_of_order = reason or None
        result = Result(code="out_of_order" if reason else "back_in_service", changed=True)
        if not reason:
            self._promote(state, now, result)
        return self._done(result)

    # -- time ------------------------------------------------------------------

    def tick(self, state: State, now: float) -> Result:
        cfg = self.cfg
        result = Result(code="tick")
        alerts = cfg.confirm_alerts_s

        for charger in list(state.chargers):
            s = charger.session
            if s is not None and not s.confirmed:
                if s.charge_start is None and now >= s.grace_ends_at:
                    s.charge_start = s.grace_ends_at
                    s.ends_at = s.charge_start + cfg.charge_s
                    result.changed = True

                if now >= s.confirm_deadline:
                    reclaimable = not state.queue
                    lost = self._end(state, charger, now, "no_show", result, close_reason="no_show")
                    result.effects.append(Notify(lost.user_id, "lost_turn", {
                        "charger_id": charger.id, "reclaimable": reclaimable,
                    }, track=lost.id if reclaimable else None))
                    if reclaimable:
                        charger.reclaim = lost
                    continue

                due = sum(1 for a in alerts if now >= s.offered_at + a)
                if due > s.alerts_sent:
                    s.alerts_sent = due
                    final = due == len(alerts)
                    result.effects.append(Notify(s.user_id, "confirm_alert", {
                        "charger_id": charger.id,
                        "final": final,
                        "confirm_deadline": s.confirm_deadline,
                        "clock_started": s.charge_start is not None,
                    }, track=s.id))
                    if final and state.queue:
                        mins = max(1, math.ceil((s.confirm_deadline - now) / 60))
                        result.effects.append(Notify(state.queue[0].user_id, "maybe_soon", {"minutes": mins}))
                    result.changed = True

            elif s is not None and s.ends_at is not None:
                if now >= s.ends_at:
                    next_user = state.queue[0].user_id if state.queue else None
                    self._end(state, charger, now, "completed", result, close_reason="completed")
                    self._close(result, charger.unplug, "unplugged", at=now)
                    charger.unplug = Unplug(user_id=s.user_id, since=now)
                    result.effects.append(Notify(s.user_id, "time_up", {
                        "charger_id": charger.id, "next_user_id": next_user,
                    }, track=f"unplug:{charger.id}"))
                elif not s.warned and now >= s.ends_at - cfg.warn_before_end_s:
                    s.warned = True
                    can_extend = not state.queue and s.extensions < cfg.max_extensions
                    result.effects.append(Notify(s.user_id, "warn_end", {
                        "charger_id": charger.id, "ends_at": s.ends_at, "can_extend": can_extend,
                    }, track=s.id))
                    result.changed = True

            # Forget stale "please unplug" reminders after a few hours.
            if charger.unplug and now - charger.unplug.since > 6 * 3600:
                self._close(result, charger.unplug, "unplugged", at=now)
                charger.unplug = None
                result.changed = True

        self._promote(state, now, result)

        etas = queue_etas(cfg, state, now)
        for entry, eta in zip(state.queue, etas):
            if entry.heads_up_sent or eta is None:
                continue
            if eta - now <= cfg.next_up_heads_up_s:
                entry.heads_up_sent = True
                result.effects.append(Notify(entry.user_id, "next_up_soon", {"eta": eta}))
                result.changed = True

        return self._done(result)


# ---------------------------------------------------------------------------
# Read model helpers shared by Slack and the web
# ---------------------------------------------------------------------------


def charger_phase(charger: Charger, now: float) -> str:
    if charger.out_of_order:
        return "out_of_order"
    s = charger.session
    if s is None:
        return "free"
    if not s.confirmed:
        return "offered"
    return "charging"
