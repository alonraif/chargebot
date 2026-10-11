from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from chargebot.config import Config
from chargebot.slack.users import Profile
from chargebot.web.server import WebApp, make_handler
from chargebot.web.tokens import check_kiosk_key, make_token, verify_token

from .conftest import Harness

SECRET = "s3cret"


def profile(uid):
    return Profile(uid, {"U1": "Noa Levi", "U2": "<script>x</script>"}.get(uid, uid), email=f"{uid}@corp.com")


@pytest.fixture
def server():
    from http.server import ThreadingHTTPServer
    h = Harness(Config(web_secret=SECRET, kiosk_key="kk"))
    app = WebApp(h.cfg, h.engine, profile)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield h, base
    srv.shutdown()


def get(url):
    try:
        with urllib.request.urlopen(url) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_tokens_round_trip_and_reject_tampering():
    tok = make_token(SECRET, "U1", now=1000)
    assert verify_token(SECRET, tok, now=1001) == "U1"
    assert verify_token(SECRET, tok, now=1000 + 25 * 3600) is None
    assert verify_token("other", tok, now=1001) is None
    payload, sig = tok.split(".")
    assert verify_token(SECRET, payload + "." + sig[:-1] + ("A" if sig[-1] != "A" else "B"), now=1001) is None
    assert verify_token(SECRET, None) is None
    assert check_kiosk_key("kk", "kk") and not check_kiosk_key("kk", "nope") and not check_kiosk_key(None, "x")


def test_pages_and_health(server):
    h, base = server
    for path in ("/", "/dashboard", "/kiosk", "/stats", "/static/app.js", "/static/app.css"):
        status, _, headers = get(base + path)
        assert status == 200, path
        assert headers.get("Referrer-Policy") == "no-referrer"
    assert get(base + "/static/../server.py")[0] == 404
    status, body, _ = get(base + "/healthz")
    assert status == 503  # scheduler hasn't ticked yet
    h.engine.tick()
    assert get(base + "/healthz")[0] == 200


def test_state_is_public_safe(server):
    h, base = server
    h.act("join", "U1")
    h.act("join", "U2")
    status, body, _ = get(base + "/api/state")
    text = body.decode()
    data = json.loads(text)
    assert status == 200
    assert "@corp.com" not in text and "U1" not in text and "U2" not in text
    assert data["chargers"][0]["user"]["name"] == "Noa Levi"
    assert data["queue"][0]["user"]["name"] == "<script>x</script>"  # escaped by the browser via textContent
    assert data["me"] is None
    assert json.loads(get(base + "/status")[1])["chargers"]


def test_magic_link_actions(server):
    h, base = server
    h.act("join", "U1")
    tok = make_token(SECRET, "U1")
    data = json.loads(get(base + f"/api/state?t={tok}")[1])
    assert data["me"]["actions"][:2] == ["confirm", "skip"]
    assert data["chargers"][0]["user"]["is_me"] is True
    status, res = post(base + "/api/action", {"action": "confirm", "t": tok})
    assert status == 200 and res["code"] == "confirmed"
    assert h.state.chargers[0].session.confirmed
    status, res = post(base + "/api/action", {"action": "confirm", "t": tok})
    assert status == 409 and res["code"] == "already_confirmed"


def test_action_rejections(server):
    h, base = server
    assert post(base + "/api/action", {"action": "confirm", "t": "bad.token"})[0] == 401
    assert post(base + "/api/action", {"action": "admin_end", "t": make_token(SECRET, "U1")})[0] == 400
    assert post(base + "/api/action", {"action": "confirm", "k": "wrong"})[0] == 403
    h.act("join", "U1")
    assert post(base + "/api/action", {"action": "skip", "k": "kk"})[0] == 403  # kiosk can only confirm


def test_kiosk_confirm(server):
    h, base = server
    assert json.loads(get(base + "/api/state?k=kk")[1])["kiosk_can_confirm"] is True
    assert json.loads(get(base + "/api/state?k=no")[1])["kiosk_can_confirm"] is False
    assert post(base + "/api/action", {"action": "confirm", "k": "kk"})[0] == 409  # nobody offered
    h.act("join", "U1")
    status, res = post(base + "/api/action", {"action": "confirm", "k": "kk", "charger_id": "c1"})
    assert status == 200 and h.state.chargers[0].session.confirmed


def test_stats_endpoint(server):
    h, base = server
    h.act("join", "U1")
    h.act("confirm", "U1")
    h.wait(30)
    h.act("done", "U1")
    data = json.loads(get(base + "/api/stats")[1])
    assert data["sessions"] == 1 and len(data["per_day"]) == 30 and len(data["heatmap"]) == 7
