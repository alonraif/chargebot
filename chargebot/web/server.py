"""Dashboard, kiosk and stats pages plus a small JSON API, on the standard library HTTP server."""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from ..config import Config
from ..domain import State, can_extend, charger_phase, queue_etas
from ..engine import Engine
from ..push import valid_subscription
from ..stats import global_stats
from .login_codes import LoginCodes
from .tokens import check_kiosk_key, make_token, read_token

log = logging.getLogger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
PAGES = {"/": "index.html", "/dashboard": "index.html", "/kiosk": "kiosk.html", "/stats": "stats.html",
         "/guide": "guide.html", "/help": "guide.html"}
# Served from the site root so the service worker can control every page.
ROOT_FILES = {"/manifest.webmanifest": "manifest.webmanifest", "/sw.js": "sw.js"}
mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("application/javascript", ".js")
WEB_ACTIONS = ("join", "confirm", "skip", "done", "unplugged", "leave", "extend", "nudge", "reclaim")
MAX_BODY = 4096


def _hue(user_id: str) -> int:
    return int(hashlib.sha256(user_id.encode()).hexdigest()[:4], 16) % 360


def _initials(name: str) -> str:
    parts = [p for p in name.replace(".", " ").split() if p]
    if not parts:
        return "?"
    return (parts[0][0] + (parts[1][0] if len(parts) > 1 else "")).upper()


class WebApp:
    def __init__(self, cfg: Config, engine: Engine, profile: Callable[[str], Any],
                 socket_connected: Callable[[], bool] = lambda: True,
                 codes: Optional[LoginCodes] = None, pusher: Any = None) -> None:
        self.cfg = cfg
        self.engine = engine
        self.profile = profile
        self.socket_connected = socket_connected
        self.codes = codes or LoginCodes()
        self.pusher = pusher

    # -- sign-in -------------------------------------------------------------------------

    def viewer(self, token: Optional[str]) -> Optional[str]:
        """The Slack user behind a dashboard pass, if it's genuine, unexpired and not signed out."""
        found = read_token(self.cfg.web_secret, token)
        if not found:
            return None
        user_id, gen = found
        if gen != int(self.engine.pref(user_id, "token_gen", 0)):
            return None
        return user_id

    def device_pass(self, token: Optional[str]) -> Dict[str, Any]:
        """Swap a short Slack link for a long-lived pass kept on this device."""
        user_id = self.viewer(token)
        if not user_id:
            return {"ok": False, "code": "bad_token", "status": 401}
        return self._issue_pass(user_id)

    def push_subscription(self, token: Optional[str], body: Dict[str, Any], on: bool) -> Dict[str, Any]:
        user_id = self.viewer(token)
        if not user_id:
            return {"ok": False, "code": "bad_token", "status": 401}
        if not self.pusher or not self.pusher.ready:
            return {"ok": False, "code": "push_unavailable", "status": 503}
        sub = valid_subscription(body.get("subscription"))
        if not sub:
            return {"ok": False, "code": "bad_subscription", "status": 400}
        if on:
            self.engine.push_subscribe(user_id, sub)
            self.pusher.send_test(user_id, sub)
        else:
            self.engine.push_unsubscribe(sub["endpoint"])
        return {"ok": True, "code": "push_on" if on else "push_off", "status": 200}

    def login_with_code(self, code: Optional[str], client: str) -> Dict[str, Any]:
        user_id, reason = self.codes.redeem(code, client)
        if not user_id:
            return {"ok": False, "code": reason, "status": 429 if reason == "too_many_attempts" else 401}
        return self._issue_pass(user_id)

    def _issue_pass(self, user_id: str) -> Dict[str, Any]:
        gen = int(self.engine.pref(user_id, "token_gen", 0))
        ttl = self.cfg.device_link_days * 86400
        return {"ok": True, "token": make_token(self.cfg.web_secret, user_id, ttl, gen=gen),
                "days": self.cfg.device_link_days, "status": 200}

    # -- read models -----------------------------------------------------------------

    def person(self, user_id: Optional[str], viewer: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if not user_id:
            return None
        p = self.profile(user_id)
        return {"name": p.name, "avatar": p.avatar, "initials": _initials(p.name), "hue": _hue(user_id),
                "is_me": user_id == viewer}

    def my_actions(self, state: State, viewer: str, now: float) -> List[str]:
        charger, session = state.session_of(viewer)
        acts: List[str] = []
        if session is not None and not session.confirmed:
            acts += ["confirm", "skip"] + (["nudge"] if charger.unplug else [])
        elif session is not None:
            acts.append("done")
            if can_extend(self.cfg, state, session, now):
                acts.append("extend")
        elif state.queue_position(viewer) is not None:
            acts.append("leave")
        else:
            if any(c.reclaim and c.reclaim.user_id == viewer and c.session is None for c in state.chargers):
                acts.append("reclaim")
            acts.append("join")
        if any(c.unplug and c.unplug.user_id == viewer for c in state.chargers):
            acts.append("unplugged")
        return acts

    def state_json(self, viewer: Optional[str] = None, kiosk: bool = False) -> Dict[str, Any]:
        state, now = self.engine.view()
        chargers = []
        for c in state.chargers:
            s = c.session
            chargers.append({
                "id": c.id,
                "name": c.name,
                "phase": charger_phase(c, now),
                "out_of_order": c.out_of_order,
                "user": self.person(s.user_id, viewer) if s else None,
                "offered_at": s.offered_at if s else None,
                "grace_ends_at": s.grace_ends_at if s else None,
                "confirm_deadline": s.confirm_deadline if s and not s.confirmed else None,
                "charge_start": s.charge_start if s else None,
                "ends_at": s.ends_at if s else None,
                "unplug_user": self.person(c.unplug.user_id, viewer) if c.unplug else None,
            })
        etas = queue_etas(self.cfg, state, now)
        queue = [{"position": i + 1, "user": self.person(e.user_id, viewer), "eta": eta}
                 for i, (e, eta) in enumerate(zip(state.queue, etas))]
        data: Dict[str, Any] = {
            "now": now,
            "version": state.version,
            "multi": self.cfg.multi_charger,
            "durations": {"charge_s": self.cfg.charge_s, "grace_s": self.cfg.grace_s,
                          "confirm_s": self.cfg.confirm_s},
            "chargers": chargers,
            "queue": queue,
            "slack_url": self.slack_url(),
            "me": None,
            "kiosk_can_confirm": kiosk,
            "push_key": self.pusher.public_key if self.pusher and self.pusher.ready else None,
        }
        if viewer:
            data["me"] = {"person": self.person(viewer, viewer), "actions": self.my_actions(state, viewer, now)}
        return data

    def slack_url(self) -> Optional[str]:
        if not self.cfg.slack_app_id:
            return None
        url = f"https://slack.com/app_redirect?app={self.cfg.slack_app_id}"
        if self.cfg.slack_team_id:
            url += f"&team={self.cfg.slack_team_id}"
        return url

    def health(self) -> Dict[str, Any]:
        _, now = self.engine.view()
        last = self.engine.last_tick
        tick_ok = last is not None and now - last < max(60.0, self.cfg.tick_s * 6)
        socket_ok = bool(self.socket_connected())
        return {"ok": tick_ok and socket_ok, "slack_connected": socket_ok, "scheduler_ok": tick_ok}

    # -- actions ---------------------------------------------------------------------

    def action(self, body: Dict[str, Any]) -> Dict[str, Any]:
        action = body.get("action")
        if action not in WEB_ACTIONS:
            return {"ok": False, "code": "bad_action", "status": 400}

        if body.get("k") is not None:
            if action != "confirm" or not check_kiosk_key(self.cfg.kiosk_key, body.get("k")):
                return {"ok": False, "code": "forbidden", "status": 403}
            state, _ = self.engine.view()
            charger = state.charger(str(body.get("charger_id") or state.chargers[0].id))
            if not charger or not charger.session or charger.session.confirmed:
                return {"ok": False, "code": "not_offered", "status": 409}
            result = self.engine.act("confirm", charger.session.user_id)
        else:
            user = self.viewer(body.get("t"))
            if not user:
                return {"ok": False, "code": "bad_token", "status": 401}
            result = self.engine.act(action, user)
        return {"ok": result.ok, "code": result.code, "data": _jsonable(result.data),
                "status": 200 if result.ok else 409}


def _jsonable(data: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in data.items() if isinstance(v, (str, int, float, bool, type(None)))
            and k not in ("user_id", "target_user_id")}


def make_handler(app: WebApp):
    static_files = set(os.listdir(STATIC_DIR)) if os.path.isdir(STATIC_DIR) else set()

    class Handler(BaseHTTPRequestHandler):
        server_version = "ChargeBot"

        def log_message(self, fmt, *args):  # noqa: N802 - keep request logs out of INFO
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _send(self, status: int, body: bytes, content_type: str, cache: str = "no-store") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, data: Any) -> None:
            self._send(status, json.dumps(data, separators=(",", ":")).encode(), "application/json")

        def _file(self, name: str, cache: str = "no-cache") -> None:
            path = os.path.join(STATIC_DIR, name)
            try:
                with open(path, "rb") as f:
                    body = f.read()
            except OSError:
                self._json(404, {"error": "not_found"})
                return
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript",):
                ctype += "; charset=utf-8"
            self._send(200, body, ctype, cache)

        def _client(self) -> str:
            # Render sits behind a proxy; the first forwarded address is the real client.
            forwarded = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            return forwarded or self.client_address[0]

        def _token(self, query: Dict[str, List[str]]) -> Optional[str]:
            return self.headers.get("X-CB-Token") or (query.get("t") or [None])[0]

        def do_HEAD(self):  # noqa: N802
            self.do_GET()

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            query = parse_qs(url.query)
            path = url.path.rstrip("/") or "/"
            try:
                if path in PAGES:
                    self._file(PAGES[path])
                elif path in ROOT_FILES:
                    self._file(ROOT_FILES[path])
                elif path.startswith("/static/") and path[len("/static/"):] in static_files:
                    self._file(path[len("/static/"):])
                elif path == "/api/state":
                    viewer = app.viewer(self._token(query))
                    kiosk = check_kiosk_key(app.cfg.kiosk_key, (query.get("k") or [None])[0])
                    self._json(200, app.state_json(viewer=viewer, kiosk=kiosk))
                elif path == "/status":
                    self._json(200, app.state_json())
                elif path == "/api/stats":
                    _, now = app.engine.view()
                    self._json(200, global_stats(app.engine.log_copy(), now, app.cfg.timezone))
                elif path == "/healthz":
                    health = app.health()
                    self._json(200 if health["ok"] else 503, health)
                else:
                    self._json(404, {"error": "not_found"})
            except Exception:
                log.exception("GET %s failed", path)
                self._json(500, {"error": "server_error"})

        def do_POST(self):  # noqa: N802
            path = urlparse(self.path).path
            if path not in ("/api/action", "/api/device", "/api/login", "/api/push/subscribe", "/api/push/unsubscribe"):
                self._json(404, {"error": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > MAX_BODY:
                    self._json(400, {"ok": False, "code": "bad_request"})
                    return
                body = json.loads(self.rfile.read(length).decode())
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                self._json(400, {"ok": False, "code": "bad_request"})
                return
            if self.headers.get("X-CB-Token") and not body.get("t") and "k" not in body:
                body["t"] = self.headers.get("X-CB-Token")
            try:
                if path == "/api/device":
                    result = app.device_pass(body.get("t"))
                elif path == "/api/login":
                    result = app.login_with_code(body.get("code"), self._client())
                elif path.startswith("/api/push/"):
                    result = app.push_subscription(body.get("t"), body, on=path.endswith("/subscribe"))
                else:
                    result = app.action(body)
            except Exception:
                log.exception("POST action failed")
                self._json(500, {"ok": False, "code": "server_error"})
                return
            self._json(result.pop("status"), result)

    return Handler


def start_server(app: WebApp, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(app))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
    log.info("Web server listening on port %d.", port)
    return server
