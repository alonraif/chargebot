from __future__ import annotations

import json
import threading
import time
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
    h.web = app
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


def test_old_two_part_passes_still_work():
    import base64
    import hashlib
    import hmac
    payload = base64.urlsafe_b64encode(b"U1|9999999999").rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).digest()[:18])
    from chargebot.web.tokens import read_token
    assert read_token(SECRET, payload + "." + sig.rstrip(b"=").decode()) == ("U1", 0)


def post_with_header(url, body, token):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-CB-Token": token})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def get_with_header(url, token):
    req = urllib.request.Request(url, headers={"X-CB-Token": token})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def test_device_pass_is_long_lived_and_works_by_header(server):
    from chargebot.web.tokens import read_token
    h, base = server
    status, res = post(base + "/api/device", {"t": make_token(SECRET, "U1")})
    assert status == 200 and res["days"] == 90
    device = res["token"]
    assert read_token(SECRET, device, now=time.time() + 80 * 86400)[0] == "U1"
    assert get_with_header(base + "/api/state", device)["me"]["person"]["name"] == "Noa Levi"
    status, res = post_with_header(base + "/api/action", {"action": "join"}, device)
    assert status == 200 and res["code"] == "offered"
    assert post(base + "/api/device", {"t": "nope.nope"})[0] == 401


def test_sign_out_everywhere_revokes_every_pass(server):
    h, base = server
    phone = post(base + "/api/device", {"t": make_token(SECRET, "U1")})[1]["token"]
    laptop = post(base + "/api/device", {"t": make_token(SECRET, "U1")})[1]["token"]
    assert get_with_header(base + "/api/state", phone)["me"] and get_with_header(base + "/api/state", laptop)["me"]
    h.engine.set_pref("U1", "token_gen", 1)  # what the Slack Home button does
    assert get_with_header(base + "/api/state", phone)["me"] is None
    assert get_with_header(base + "/api/state", laptop)["me"] is None
    assert post_with_header(base + "/api/action", {"action": "join"}, phone)[0] == 401
    fresh = post(base + "/api/device", {"t": make_token(SECRET, "U1", gen=1)})[1]["token"]
    assert get_with_header(base + "/api/state", fresh)["me"]


def test_installable_app_files(server):
    h, base = server
    status, body, headers = get(base + "/manifest.webmanifest")
    manifest = json.loads(body)
    assert status == 200 and headers["Content-Type"].startswith("application/manifest+json")
    assert manifest["display"] == "standalone" and {i["sizes"] for i in manifest["icons"]} >= {"192x192", "512x512"}
    for icon in manifest["icons"]:
        assert get(base + icon["src"])[0] == 200
    status, _, headers = get(base + "/sw.js")
    assert status == 200 and "javascript" in headers["Content-Type"]
    page = get(base + "/")[1].decode()
    assert 'rel="manifest"' in page and "installBtn" in page


def test_login_codes_are_single_use_expire_and_rate_limit():
    from chargebot.web.login_codes import CODE_TTL_S, MAX_FAILS_PER_CLIENT, LoginCodes
    now = [1000.0]
    codes = LoginCodes(clock=lambda: now[0])
    code = codes.issue("U1")
    assert len(code) == 7 and code[3] == "-"
    assert codes.redeem(code.lower().replace("-", " "), "ip1") == ("U1", "ok")  # forgiving input
    assert codes.redeem(code, "ip1") == (None, "bad_code")  # used up
    older = codes.issue("U1")
    newer = codes.issue("U1")
    assert codes.redeem(older, "ip1")[1] == "bad_code"  # a new code replaces the old one
    now[0] += CODE_TTL_S + 1
    assert codes.redeem(newer, "ip1")[1] == "bad_code"  # expired
    for _ in range(MAX_FAILS_PER_CLIENT):
        codes.redeem("AAAAAA", "ip2")
    fresh = codes.issue("U2")
    assert codes.redeem(fresh, "ip2") == (None, "too_many_attempts")
    assert codes.redeem(fresh, "ip3") == ("U2", "ok")  # other clients unaffected


def test_login_endpoint_issues_device_pass(server):
    h, base = server
    code = h.web.codes.issue("U1")
    status, res = post(base + "/api/login", {"code": code})
    assert status == 200 and res["ok"] and res["days"] == 90
    assert get_with_header(base + "/api/state", res["token"])["me"]["person"]["name"] == "Noa Levi"
    status, res = post(base + "/api/login", {"code": code})
    assert status == 401 and res["code"] == "bad_code"


class FakePusher:
    ready = True
    public_key = "BPUBLICKEY"

    def __init__(self):
        self.tests = []

    def send_test(self, user_id, sub):
        self.tests.append((user_id, sub["endpoint"]))


def test_push_subscribe_endpoints(server):
    h, base = server
    pusher = FakePusher()
    h.web.pusher = pusher
    device = post(base + "/api/login", {"code": h.web.codes.issue("U1")})[1]["token"]
    assert get_with_header(base + "/api/state", device)["push_key"] == "BPUBLICKEY"
    good = {"endpoint": "https://web.push.apple.com/abc",
            "keys": {"p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQtUbVlUls0VJXg7A8u-Ts1Xbjh",
                     "auth": "tBHItJI5svbpez7KI4CCXg"}}
    assert post(base + "/api/push/subscribe", {"subscription": good})[0] == 401  # must be signed in
    status, res = post_with_header(base + "/api/push/subscribe", {"subscription": good}, device)
    assert status == 200 and pusher.tests == [("U1", good["endpoint"])]
    assert h.engine.pref("U1", "push")[0]["endpoint"] == good["endpoint"]
    evil = dict(good, endpoint="https://evil.example.com/x")
    assert post_with_header(base + "/api/push/subscribe", {"subscription": evil}, device)[0] == 400
    assert post_with_header(base + "/api/push/unsubscribe", {"subscription": good}, device)[0] == 200
    assert h.engine.pref("U1", "push") == []
    h.web.pusher = None
    assert post_with_header(base + "/api/push/subscribe", {"subscription": good}, device)[0] == 503
