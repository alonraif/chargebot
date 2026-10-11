"""Drives the real Bolt handlers with Slack-shaped payloads against a fake Slack API server."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest
from slack_bolt import App
from slack_bolt.request import BoltRequest
from slack_sdk import WebClient

from chargebot.config import Config
from chargebot.slack import blocks as B
from chargebot.slack.handlers import register
from chargebot.slack.notifier import Notifier
from chargebot.slack.users import UserDirectory

from .conftest import Harness


class FakeSlackServer:
    def __init__(self):
        self.calls = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = {k: v[0] for k, v in parse_qs(raw).items()}
                outer.calls.append((self.path, body))
                reply = {"ok": True, "ts": f"{time.time():.6f}", "channel": "D123",
                         "user": {"id": "U1", "real_name": "Noa Levi", "profile": {"display_name": "Noa"}}}
                data = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def paths(self):
        return [p for p, _ in self.calls]

    def wait_for(self, path, count=1, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if sum(1 for p in self.paths() if p.endswith(path)) >= count:
                return True
            time.sleep(0.02)
        return False


@pytest.fixture
def bot():
    fake = FakeSlackServer()
    client = WebClient(token="xoxb-test", base_url=fake.url)
    app = App(client=client, signing_secret="x", token_verification_enabled=False,
              request_verification_enabled=False, process_before_response=True)
    h = Harness(Config(web_secret="t", admin_user_ids=frozenset({"UA"}), public_base_url="https://cb.example"))
    users = UserDirectory(client)
    notifier = Notifier(h.cfg, client, h.engine, users, h.store)
    h.engine.set_dispatcher(notifier.dispatch)
    notifier.start()
    register(app, h.cfg, h.engine, notifier, users)
    yield app, h, fake
    fake.server.shutdown()


def dispatch(app, body):
    resp = app.dispatch(BoltRequest(body=body, mode="socket_mode"))
    assert resp.status == 200, resp.body
    return resp


def command(app, fake, name, text="", user="U1"):
    return dispatch(app, {"type": "slash_commands", "command": name, "text": text, "user_id": user,
                          "user_name": "noa", "team_id": "T1", "channel_id": "C1", "trigger_id": "trig",
                          "response_url": fake.url + "respond"})


def block_action(app, fake, action_id, user="U1", value="x", container=None, view=None, selected=None):
    action = {"action_id": action_id, "block_id": "b", "type": "button", "value": value}
    if selected:
        action = {"action_id": action_id, "block_id": "b", "type": "overflow", "selected_option": {"value": selected}}
    body = {"type": "block_actions", "user": {"id": user}, "team": {"id": "T1"}, "api_app_id": "A1",
            "trigger_id": "trig", "actions": [action],
            "container": container or {"type": "message", "message_ts": "1.1", "channel_id": "D123"},
            "response_url": fake.url + "respond"}
    if view:
        body["view"] = view
    return dispatch(app, body)


def test_charge_command_offers_and_dms(bot):
    app, h, fake = bot
    command(app, fake, "/charge")
    assert h.state.chargers[0].session.user_id == "U1"
    assert fake.wait_for("/respond")
    assert fake.wait_for("chat.postMessage")  # the offer DM
    post = next(b for p, b in fake.calls if p.endswith("chat.postMessage"))
    assert "cb.example/?t=" in json.dumps(post)  # magic link
    # The DM's ts gets attached so its buttons can be closed later.
    deadline = time.time() + 2
    while not h.state.chargers[0].session.messages and time.time() < deadline:
        time.sleep(0.02)
    assert h.state.chargers[0].session.messages


def test_legacy_commands_still_work(bot):
    app, h, fake = bot
    command(app, fake, "/request", user="U1")
    command(app, fake, "/request", user="U2")
    command(app, fake, "/chargestatus", user="U3")
    command(app, fake, "/exitqueue", user="U2")
    command(app, fake, "/endcharge", user="U1")
    assert not h.state.queue and h.state.chargers[0].session is None
    assert fake.wait_for("/respond", count=5)
    responses = [b for p, b in fake.calls if p.endswith("/respond")]
    assert all("/charge" in json.dumps(r) for r in responses)  # each legacy reply carries the tip


def test_confirm_button_in_dm_replaces_message(bot):
    app, h, fake = bot
    h.act("join", "U1")
    block_action(app, fake, B.A_CONFIRM)
    assert h.state.chargers[0].session.confirmed
    assert fake.wait_for("/respond")
    reply = [b for p, b in fake.calls if p.endswith("/respond")][-1]
    assert reply.get("replace_original") in (True, "true")


def test_failed_button_answers_ephemerally(bot):
    app, h, fake = bot
    block_action(app, fake, B.A_CONFIRM, user="U9")
    assert fake.wait_for("/respond")
    reply = [b for p, b in fake.calls if p.endswith("/respond")][-1]
    assert reply.get("replace_original") in (False, "false") and reply.get("response_type") == "ephemeral"


def test_live_message_button_never_replaces_it(bot):
    app, h, fake = bot
    h.engine.set_meta("status_ts", "9.9")
    block_action(app, fake, B.A_JOIN, user="U2", container={"type": "message", "message_ts": "9.9", "channel_id": "C1"})
    assert fake.wait_for("/respond")
    reply = [b for p, b in fake.calls if p.endswith("/respond")][-1]
    assert reply.get("replace_original") in (False, "false")


def test_home_tab_open_and_button(bot):
    app, h, fake = bot
    dispatch(app, {"type": "event_callback", "team_id": "T1", "api_app_id": "A1",
                   "event": {"type": "app_home_opened", "user": "U1", "tab": "home"}})
    assert fake.wait_for("views.publish")
    block_action(app, fake, B.A_JOIN, container={"type": "view", "view_id": "V1"},
                 view={"id": "V1", "type": "home"})
    assert fake.wait_for("views.publish", count=2)
    assert h.state.chargers[0].session.user_id == "U1"


def test_shortcut_opens_panel(bot):
    app, h, fake = bot
    dispatch(app, {"type": "shortcut", "callback_id": "ev_charger_open", "user": {"id": "U1"},
                   "team": {"id": "T1"}, "trigger_id": "trig"})
    assert fake.wait_for("views.open")


def test_admin_requires_admin(bot):
    app, h, fake = bot
    command(app, fake, "/charge", "admin", user="U1")
    assert fake.wait_for("/respond")
    assert not any(p.endswith("views.open") for p in fake.paths())
    command(app, fake, "/charge", "admin", user="UA")
    assert fake.wait_for("views.open")


def test_admin_out_of_order_and_queue_tools(bot):
    app, h, fake = bot
    command(app, fake, "/charge", "admin out Broken cable", user="UA")
    assert h.state.chargers[0].out_of_order == "Broken cable"
    command(app, fake, "/charge", "admin back", user="UA")
    assert h.state.chargers[0].out_of_order is None
    for u in ("U1", "U2", "U3"):
        h.act("join", u)
    view = {"id": "V9", "type": "modal"}
    block_action(app, fake, B.A_ADMIN_QUEUE, user="UA", container={"type": "view", "view_id": "V9"}, view=view,
                 selected="up:U3")
    assert [q.user_id for q in h.state.queue] == ["U3", "U2"]
    block_action(app, fake, B.A_ADMIN_END, user="UA", value="c1", container={"type": "view"}, view=view)
    assert h.state.chargers[0].session.user_id == "U3"
    block_action(app, fake, B.A_ADMIN_END, user="U2", value="c1", container={"type": "view"}, view=view)
    assert h.state.chargers[0].session.user_id == "U3"  # non-admin ignored


def test_unknown_subcommand_shows_help(bot):
    app, h, fake = bot
    command(app, fake, "/charge", "dance")
    assert fake.wait_for("/respond")
    assert "know `dance`" in json.dumps([b for p, b in fake.calls if p.endswith("/respond")][-1])


def test_production_app_builds_without_signing_secret(monkeypatch):
    from chargebot.__main__ import make_app
    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)
    app = make_app(Config(slack_bot_token="xoxb-test"), token_verification_enabled=False)
    h = Harness(Config(web_secret="t"))
    users = UserDirectory(app.client)
    register(app, h.cfg, h.engine, Notifier(h.cfg, app.client, h.engine, users, h.store), users)


def test_login_command_and_home_code_button(bot):
    app, h, fake = bot
    command(app, fake, "/charge", "login")
    assert fake.wait_for("/respond")
    text = json.dumps([b for p, b in fake.calls if p.endswith("/respond")][-1])
    assert "sign-in code" in text
    block_action(app, fake, B.A_LOGIN_CODE, container={"type": "view", "view_id": "V1"},
                 view={"id": "V1", "type": "home"})
    assert fake.wait_for("views.open")


def test_live_message_offers_personal_dashboard_link(bot):
    app, h, fake = bot
    state, now = h.engine.view()
    _, live = B.live_message(state, now, h.cfg, lambda u: u, "https://cb.example/")
    ids = [e["action_id"] for b in live if b["type"] == "actions" for e in b["elements"]]
    assert B.A_MY_DASHBOARD in ids and B.A_LINK not in ids  # no shared, sign-in-less link
    block_action(app, fake, B.A_MY_DASHBOARD, user="U2",
                 container={"type": "message", "message_ts": "9.9", "channel_id": "C1"})
    assert fake.wait_for("/respond")
    reply = [b for p, b in fake.calls if p.endswith("/respond")][-1]
    assert reply.get("response_type") == "ephemeral" and "cb.example/?t=" in json.dumps(reply)


def test_dashboard_command_gives_personal_link(bot):
    app, h, fake = bot
    command(app, fake, "/charge", "dashboard")
    assert fake.wait_for("/respond")
    assert "cb.example/?t=" in json.dumps([b for p, b in fake.calls if p.endswith("/respond")][-1])
