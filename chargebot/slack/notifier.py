"""Turns domain effects into Slack and email calls, on background threads."""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Dict, List, Optional

from slack_sdk.errors import SlackApiError

from ..config import Config
from ..domain import CancelInvite, CloseMessages, LogSession, Notify, SendInvite, StatusChanged
from ..engine import Engine
from ..reminders.gmail import GmailSender
from ..web.tokens import make_token
from . import blocks as B
from .users import UserDirectory

log = logging.getLogger(__name__)

HOME_VIEWER_TTL_S = 3 * 3600
PERIODIC_REFRESH_S = 5 * 60
STATUS_DEBOUNCE_S = 1.5


class Notifier:
    def __init__(self, cfg: Config, client: Any, engine: Engine, users: UserDirectory, store: Any,
                 gmail: Optional[GmailSender] = None, pusher: Any = None) -> None:
        self.cfg = cfg
        self.client = client
        self.engine = engine
        self.users = users
        self.store = store
        self.gmail = gmail
        self.pusher = pusher
        self._slack_q: "queue.Queue[Any]" = queue.Queue()
        self._email_q: "queue.Queue[Any]" = queue.Queue()
        self._status_dirty = threading.Event()
        self._home_viewers: Dict[str, float] = {}
        self._viewers_lock = threading.Lock()

    def start(self) -> None:
        for target, name in ((self._slack_loop, "notify-slack"), (self._email_loop, "notify-email"),
                             (self._status_loop, "notify-status")):
            threading.Thread(target=target, name=name, daemon=True).start()
        self._status_dirty.set()

    # -- public ------------------------------------------------------------------

    def dispatch(self, effects: List[Any]) -> None:
        for effect in effects:
            if isinstance(effect, (SendInvite, CancelInvite)):
                self._email_q.put(effect)
            elif isinstance(effect, StatusChanged):
                self._status_dirty.set()
            else:
                self._slack_q.put(effect)
                if self.pusher is not None and isinstance(effect, Notify):
                    try:
                        self.pusher.notify(effect)
                    except Exception:
                        log.exception("Queueing push notification failed.")

    def link_for(self, user_id: Optional[str]) -> Optional[str]:
        if not self.cfg.public_base_url:
            return None
        if not user_id:
            return self.cfg.public_base_url + "/"
        gen = int(self.engine.pref(user_id, "token_gen", 0))
        return f"{self.cfg.public_base_url}/?t={make_token(self.cfg.web_secret, user_id, gen=gen)}"

    def note_home_open(self, user_id: str) -> None:
        with self._viewers_lock:
            self._home_viewers[user_id] = time.time()

    def publish_home(self, user_id: str, notice: Optional[str] = None) -> None:
        state, now = self.engine.view()
        view = B.home_view(state, now, self.cfg, self.users.name, user_id, self.link_for(user_id),
                           self.engine.log_copy(), notice)
        try:
            self.client.views_publish(user_id=user_id, view=view)
        except SlackApiError as e:
            log.warning("views.publish for %s failed: %s", user_id, e.response.get("error"))

    # -- loops ----------------------------------------------------------------------

    def _slack_loop(self) -> None:
        while True:
            effect = self._slack_q.get()
            try:
                if isinstance(effect, Notify):
                    self._notify(effect)
                elif isinstance(effect, CloseMessages):
                    self._close(effect)
                elif isinstance(effect, LogSession):
                    self._log(effect)
            except Exception:
                log.exception("Handling %s failed.", type(effect).__name__)

    def _email_loop(self) -> None:
        while True:
            effect = self._email_q.get()
            try:
                self._email(effect)
            except Exception:
                log.exception("Calendar reminder for %s failed.", getattr(effect, "user_id", "?"))

    def _status_loop(self) -> None:
        while True:
            self._status_dirty.wait(PERIODIC_REFRESH_S)
            time.sleep(STATUS_DEBOUNCE_S)
            self._status_dirty.clear()
            try:
                self._refresh_live_message()
                self._refresh_homes()
            except Exception:
                log.exception("Status refresh failed.")

    # -- handlers -------------------------------------------------------------------

    def _notify(self, effect: Notify) -> None:
        state, _ = self.engine.view()
        data = dict(effect.data, user_id=effect.user_id)
        text, blocks = B.notification(effect.kind, data, self.cfg, state, self.users.name,
                                      self.link_for(effect.user_id))
        resp = self.client.chat_postMessage(channel=effect.user_id, text=text, blocks=blocks)
        if effect.track:
            self.engine.attach_message(effect.track, resp["channel"], resp["ts"])

    def _close(self, effect: CloseMessages) -> None:
        text, blocks = B.closed(effect.reason, effect.data)
        for ref in effect.refs:
            try:
                self.client.chat_update(channel=ref["channel"], ts=ref["ts"], text=text, blocks=blocks)
            except SlackApiError as e:
                log.info("Could not close message %s: %s", ref.get("ts"), e.response.get("error"))

    def _log(self, effect: LogSession) -> None:
        r = effect.record
        name = self.users.name(r.get("user_id"))
        when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(r.get("ended_at") or time.time()))
        labels = {"completed": "charged", "ended_early": "ended early", "kicked": "ended by admin",
                  "no_show": "no-show", "skipped": "skipped"}
        summary = f"{name}: {labels.get(r.get('outcome'), r.get('outcome'))} ({when})"
        self.store.append_log(r, summary)

    def _email(self, effect: Any) -> None:
        if not self.gmail or not self.gmail.ready:
            return
        email = self.users.get(effect.user_id).email
        if not email:
            log.info("No email on file for %s; skipping calendar reminder.", effect.user_id)
            return
        self.gmail.send(email, effect.uid, effect.start, effect.end, effect.sequence,
                        cancel=isinstance(effect, CancelInvite))

    def _refresh_live_message(self) -> None:
        channel = self.cfg.status_channel_id
        if not channel:
            return
        state, now = self.engine.view()
        text, blocks = B.live_message(state, now, self.cfg, self.users.name, self.link_for(None))
        ts = state.meta.get("status_ts")
        if ts and state.meta.get("status_channel") == channel:
            try:
                self.client.chat_update(channel=channel, ts=ts, text=text, blocks=blocks)
                return
            except SlackApiError as e:
                log.warning("Live status message update failed (%s); posting a new one.", e.response.get("error"))
        resp = self.client.chat_postMessage(channel=channel, text=text, blocks=blocks)
        self.engine.set_meta("status_ts", resp["ts"])
        self.engine.set_meta("status_channel", channel)
        try:
            self.client.pins_add(channel=channel, timestamp=resp["ts"])
        except SlackApiError as e:
            log.info("Could not pin the live status message (%s). Pin it by hand if you like.",
                     e.response.get("error"))

    def _refresh_homes(self) -> None:
        cutoff = time.time() - HOME_VIEWER_TTL_S
        with self._viewers_lock:
            for uid in [u for u, t in self._home_viewers.items() if t < cutoff]:
                del self._home_viewers[uid]
            viewers = list(self._home_viewers)[:100]
        for uid in viewers:
            self.publish_home(uid)
