"""Web Push notifications to the installed dashboard app (phones and desktops), alongside Slack DMs.

Each device that turns notifications on stores a push subscription with the user's prefs. When the
domain asks for a DM (a Notify effect), the same moment is also pushed to that user's devices.
"""

from __future__ import annotations

import base64
import json
import logging
import queue
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from .config import Config
from .domain import Notify
from .web.tokens import make_token

log = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

# Only the browsers' real push services: the server never posts to an arbitrary address.
PUSH_HOSTS = ("fcm.googleapis.com", "push.services.mozilla.com", "push.apple.com", "notify.windows.com")
MAX_DEVICES_PER_USER = 5
ACTION_TOKEN_TTL_S = 2 * 3600


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def valid_subscription(sub: Any) -> Optional[Dict[str, str]]:
    """The parts of a browser PushSubscription we keep, or None if it isn't a real push service."""
    if not isinstance(sub, dict):
        return None
    endpoint = sub.get("endpoint")
    keys = sub.get("keys") or {}
    if not isinstance(endpoint, str) or len(endpoint) > 1000 or not isinstance(keys, dict):
        return None
    url = urlparse(endpoint)
    host = (url.hostname or "").lower()
    if url.scheme != "https" or not any(host == h or host.endswith("." + h) for h in PUSH_HOSTS):
        return None
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    ok = all(isinstance(k, str) and 10 <= len(k) <= 200 and all(c.isalnum() or c in "-_=" for c in k)
             for k in (p256dh, auth))
    return {"endpoint": endpoint, "p256dh": p256dh, "auth": auth} if ok else None


def generate_keys() -> Dict[str, str]:
    from py_vapid import Vapid
    v = Vapid()
    v.generate_keys()
    raw = v.private_key.private_numbers().private_value.to_bytes(32, "big")
    return {"VAPID_PRIVATE_KEY": _b64(raw), "VAPID_PUBLIC_KEY": public_key_for(_b64(raw))}


def public_key_for(private_key: str) -> str:
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid
    pub = Vapid.from_raw(private_key.encode()).public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return _b64(pub)


def message(kind: str, data: Dict[str, Any], cfg: Config,
            names: Callable[[Optional[str]], str]) -> Optional[Dict[str, Any]]:
    """Plain-text notification for a DM kind (None for ones not worth a push)."""
    tz = timezone.utc
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo(cfg.timezone)
        except Exception:
            pass

    def at(ts: Optional[float]) -> str:
        return datetime.fromtimestamp(ts, tz).strftime("%H:%M") if ts else "soon"

    def note(title: str, body: str, tag: str, ttl: int, actions: Optional[List[Dict]] = None,
             sticky: bool = False) -> Dict[str, Any]:
        msg: Dict[str, Any] = {"title": title, "body": body, "tag": tag, "ttl": ttl}
        if actions:
            msg["actions"] = actions
        if sticky:
            msg["requireInteraction"] = True
        return msg

    confirm = {"action": "confirm", "title": "I'm plugged in"}
    unplugged = {"action": "unplugged", "title": "I've unplugged"}
    deadline = at(data.get("confirm_deadline"))

    if kind == "offered":
        return note("⚡ Your turn!", f"The charger is yours. Plug in and confirm by {deadline}.", "cb-turn", 600,
                    [confirm, {"action": "skip", "title": "Skip"}], sticky=True)
    if kind == "confirm_alert":
        body = (f"Last call: confirm by {deadline} or the charger goes to the next person." if data.get("final")
                else f"Your charging clock has started. Confirm by {deadline} to keep your turn.")
        return note("⏰ Confirm you've plugged in", body, "cb-turn", 600, [confirm], sticky=True)
    if kind == "maybe_soon":
        return note("👀 Heads-up", f"You might be up in about {data.get('minutes')} min.", "cb-soon", 300)
    if kind == "lost_turn":
        if data.get("reclaimable"):
            return note("⌛ Your turn passed", "Nobody else is waiting, so you can still take it.", "cb-turn", 600,
                        [{"action": "reclaim", "title": "Reclaim it"}], sticky=True)
        return note("⌛ Your turn passed", "It wasn't confirmed in time, so the charger went to the next person.",
                    "cb-turn", 3600)
    if kind == "warn_end":
        actions = [{"action": "done", "title": "I'm done"}]
        if data.get("can_extend"):
            actions.append({"action": "extend", "title": "Extend"})
        return note("⏳ 10 minutes left", f"Your session ends at {at(data.get('ends_at'))}.", "cb-session", 600,
                    actions)
    if kind == "time_up":
        nxt = data.get("next_user_id")
        tail = f" {names(nxt)} is up next." if nxt else ""
        return note("🏁 Time's up", "Unplug and move your car." + tail, "cb-session", 3600, [unplugged], sticky=True)
    if kind == "nudge":
        who = names(data.get("from_user_id"))
        return note("🔔 Someone's waiting", f"{who} says your car is still plugged in.", "cb-session", 3600,
                    [unplugged], sticky=True)
    if kind == "next_up_soon":
        return note("🔜 You're up soon", f"The charger should free up around {at(data.get('eta'))}.", "cb-soon", 900)
    if kind == "kicked":
        return note("🛑 Session ended", "An admin ended your charging session.", "cb-session", 3600)
    return None  # "charging", "removed": the person just acted, or it's covered in Slack


class PushSender:
    def __init__(self, cfg: Config, engine: Any, names: Callable[[Optional[str]], str]) -> None:
        self.cfg = cfg
        self.engine = engine
        self.names = names
        self.public_key: Optional[str] = None
        self._vapid = None
        self._q: "queue.Queue[Any]" = queue.Queue()
        if cfg.vapid_private_key:
            try:
                from py_vapid import Vapid
                self._vapid = Vapid.from_raw(cfg.vapid_private_key.encode())
                self.public_key = public_key_for(cfg.vapid_private_key)
            except Exception:
                log.exception("VAPID_PRIVATE_KEY is invalid; push notifications are off.")

    @property
    def ready(self) -> bool:
        return self._vapid is not None

    def start(self) -> None:
        if self.ready:
            threading.Thread(target=self._run, name="push", daemon=True).start()

    def notify(self, effect: Notify) -> None:
        if not self.ready:
            return
        subs: List[Dict[str, str]] = self.engine.pref(effect.user_id, "push", []) or []
        if not subs:
            return
        data = dict(effect.data, user_id=effect.user_id)
        msg = message(effect.kind, data, self.cfg, self.names)
        if msg:
            for sub in subs:
                self._q.put((effect.user_id, sub, msg))

    def send_test(self, user_id: str, sub: Dict[str, str]) -> None:
        if self.ready:
            self._q.put((user_id, sub, {"title": "🔔 Notifications are on", "tag": "cb-test", "ttl": 300,
                                        "body": "You'll get your turn, reminders and time's up right here."}))

    def _run(self) -> None:
        while True:
            user_id, sub, msg = self._q.get()
            try:
                self._send(user_id, sub, msg)
            except Exception:
                log.exception("Push to %s failed.", user_id)

    def _send(self, user_id: str, sub: Dict[str, str], msg: Dict[str, Any]) -> None:
        from pywebpush import WebPushException, webpush
        gen = int(self.engine.pref(user_id, "token_gen", 0))
        payload = {k: v for k, v in msg.items() if k != "ttl"}
        payload["url"] = "/"
        if msg.get("actions"):
            # Lets the notification's buttons act without opening the app.
            payload["token"] = make_token(self.cfg.web_secret, user_id, ACTION_TOKEN_TTL_S, gen=gen)
        try:
            webpush(
                subscription_info={"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
                data=json.dumps(payload),
                vapid_private_key=self._vapid,
                vapid_claims={"sub": self.cfg.vapid_subject},
                ttl=int(msg.get("ttl", 600)),
                headers={"Urgency": "high"},
                timeout=15,
            )
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                log.info("Push subscription for %s is gone; removing it.", user_id)
                self.engine.push_unsubscribe(sub["endpoint"])
            else:
                raise
