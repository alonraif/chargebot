"""Owns the live state. Applies rules under a lock, then persists and dispatches effects outside it."""

from __future__ import annotations

import copy
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import Config
from .domain import LogSession, Result, Rules, State, ensure_chargers, new_state

log = logging.getLogger(__name__)

USER_ACTIONS = ("join", "confirm", "skip", "done", "unplugged", "leave", "extend", "nudge", "reclaim")


class Engine:
    def __init__(self, cfg: Config, store: Any, clock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.store = store
        self.clock = clock
        self.rules = Rules(cfg)
        self.state: State = new_state(cfg)
        self.session_log: List[Dict[str, Any]] = []
        self.last_tick: Optional[float] = None
        self._lock = threading.RLock()
        self._dispatch: Callable[[List[Any]], None] = lambda effects: None

    # -- setup -----------------------------------------------------------------

    def load(self) -> None:
        try:
            snapshot = self.store.load()
        except Exception:
            log.exception("Could not load saved state; starting with an empty charger and queue.")
            snapshot = None
        with self._lock:
            if snapshot:
                try:
                    self.state = State.from_dict(snapshot)
                except Exception:
                    log.exception("Saved state is unreadable; starting empty.")
                    self.state = new_state(self.cfg)
            ensure_chargers(self.state, self.cfg)
            try:
                self.session_log = self.store.load_log()
            except Exception:
                log.exception("Could not load the session log; stats start empty.")
                self.session_log = []
        log.info("State ready: %d charger(s), %d in queue.", len(self.state.chargers), len(self.state.queue))

    def set_dispatcher(self, dispatch: Callable[[List[Any]], None]) -> None:
        self._dispatch = dispatch

    # -- applying changes --------------------------------------------------------

    def apply(self, fn: Callable[[State, float], Result]) -> Result:
        with self._lock:
            now = self.clock()
            backup = copy.deepcopy(self.state)
            try:
                result = fn(self.state, now)
            except Exception:
                self.state = backup
                raise
            snapshot = None
            if result.changed:
                self.state.version += 1
                snapshot = self.state.to_dict()
                for effect in result.effects:
                    if isinstance(effect, LogSession):
                        self.session_log.append(effect.record)
        # Everything below talks to the network, so it runs without the lock.
        if snapshot is not None:
            self._save(snapshot)
        if result.effects:
            try:
                self._dispatch(result.effects)
            except Exception:
                log.exception("Effect dispatch failed.")
        return result

    def act(self, action: str, user_id: str) -> Result:
        if action not in USER_ACTIONS:
            raise ValueError(f"Unknown action {action!r}")
        method = getattr(self.rules, action)
        return self.apply(lambda st, now: method(st, user_id, now))

    def tick(self) -> Result:
        result = self.apply(self.rules.tick)
        self.last_tick = self.clock()
        return result

    def admin_end(self, charger_id: str, by: str) -> Result:
        return self.apply(lambda st, now: self.rules.admin_end(st, charger_id, now, by))

    def admin_remove(self, user_id: str, by: str) -> Result:
        return self.apply(lambda st, now: self.rules.admin_remove(st, user_id, now, by))

    def admin_move_up(self, user_id: str) -> Result:
        return self.apply(lambda st, now: self.rules.admin_move_up(st, user_id, now))

    def set_out_of_order(self, charger_id: str, reason: Optional[str]) -> Result:
        return self.apply(lambda st, now: self.rules.set_out_of_order(st, charger_id, reason, now))

    def set_pref(self, user_id: str, key: str, value: Any) -> Result:
        return self.apply(lambda st, now: self.rules.set_pref(st, user_id, key, value))

    # -- bookkeeping that doesn't change what users see ------------------------------

    def attach_message(self, track: str, channel: str, ts: str) -> None:
        """Remember a DM that carries buttons, so it can be closed when it goes stale."""
        with self._lock:
            holder = self._find_holder(track)
            if holder is None:
                return
            holder.messages.append({"channel": channel, "ts": ts})
            self.state.version += 1
            snapshot = self.state.to_dict()
        self._save(snapshot)

    def set_meta(self, key: str, value: Any) -> None:
        with self._lock:
            if self.state.meta.get(key) == value:
                return
            self.state.meta[key] = value
            self.state.version += 1
            snapshot = self.state.to_dict()
        self._save(snapshot)

    def _find_holder(self, track: str) -> Any:
        if track.startswith("unplug:"):
            charger = self.state.charger(track.split(":", 1)[1])
            return charger.unplug if charger else None
        for charger in self.state.chargers:
            for session in (charger.session, charger.reclaim):
                if session is not None and session.id == track:
                    return session
        return None

    def _save(self, snapshot: Dict[str, Any]) -> None:
        try:
            self.store.save(snapshot)
        except Exception:
            log.exception("Saving state failed.")

    # -- reading -------------------------------------------------------------------

    def view(self) -> Tuple[State, float]:
        """A private copy of the state plus the current time, safe to read without the lock."""
        with self._lock:
            return copy.deepcopy(self.state), self.clock()

    def pref(self, user_id: str, key: str, default: Any = None) -> Any:
        with self._lock:
            return self.state.prefs.get(user_id, {}).get(key, default)

    def log_copy(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.session_log)
