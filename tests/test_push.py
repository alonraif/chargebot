from __future__ import annotations

import json

import pytest

from chargebot.config import Config
from chargebot.domain import Notify
from chargebot.push import PushSender, generate_keys, message, valid_subscription

from .conftest import Harness

KEYS = {"p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQtUbVlUls0VJXg7A8u-Ts1XbjhazAkj7I99e8QcYP7DkM",
        "auth": "tBHItJI5svbpez7KI4CCXg"}


def sub(endpoint="https://fcm.googleapis.com/fcm/send/abc123"):
    return {"endpoint": endpoint, "keys": dict(KEYS)}


@pytest.mark.parametrize("endpoint,ok", [
    ("https://fcm.googleapis.com/fcm/send/abc", True),
    ("https://web.push.apple.com/QGx1", True),
    ("https://updates.push.services.mozilla.com/wpush/v2/x", True),
    ("https://wns2-db5p.notify.windows.com/w/?token=x", True),
    ("http://fcm.googleapis.com/fcm/send/abc", False),
    ("https://evil.example.com/fcm.googleapis.com", False),
    ("https://fcm.googleapis.com.evil.com/x", False),
    ("https://169.254.169.254/latest/meta-data", False),
])
def test_only_real_push_services_are_accepted(endpoint, ok):
    assert (valid_subscription(sub(endpoint)) is not None) == ok


def test_bad_keys_rejected():
    s = sub()
    s["keys"]["auth"] = "<script>"
    assert valid_subscription(s) is None
    assert valid_subscription("nope") is None


def test_messages_for_every_kind():
    cfg = Config(web_secret="t")
    data = {"confirm_deadline": 1.79e9, "final": True, "minutes": 2, "reclaimable": True, "ends_at": 1.79e9,
            "can_extend": True, "next_user_id": "U2", "from_user_id": "U2", "eta": 1.79e9}
    for kind in ("offered", "confirm_alert", "maybe_soon", "lost_turn", "warn_end", "time_up", "nudge",
                 "next_up_soon", "kicked"):
        msg = message(kind, data, cfg, lambda u: "Dana")
        assert msg["title"] and msg["body"] and "{" not in msg["body"]
    assert message("charging", data, cfg, lambda u: "x") is None
    offered = message("offered", data, cfg, lambda u: "x")
    assert [a["action"] for a in offered["actions"]] == ["confirm", "skip"]


def test_subscriptions_are_deduped_capped_and_removed():
    h = Harness(Config(web_secret="t"))
    for i in range(7):
        h.engine.push_subscribe("U1", valid_subscription(sub(f"https://fcm.googleapis.com/fcm/send/{i}")))
    h.engine.push_subscribe("U1", valid_subscription(sub("https://fcm.googleapis.com/fcm/send/6")))
    subs = h.engine.pref("U1", "push")
    assert len(subs) == 5 and subs[-1]["endpoint"].endswith("/6")
    h.engine.push_unsubscribe("https://fcm.googleapis.com/fcm/send/6")
    assert len(h.engine.pref("U1", "push")) == 4


def test_sender_pushes_with_action_token_and_drops_gone_devices(monkeypatch):
    import pywebpush

    from chargebot.web.tokens import verify_token
    h = Harness(Config(web_secret="t", vapid_private_key=generate_keys()["VAPID_PRIVATE_KEY"]))
    sender = PushSender(h.cfg, h.engine, lambda u: "Dana")
    assert sender.ready and sender.public_key
    h.engine.push_subscribe("U1", valid_subscription(sub()))
    sent = []

    def fake_webpush(subscription_info, data, **kw):
        sent.append((subscription_info, json.loads(data), kw))

    monkeypatch.setattr(pywebpush, "webpush", fake_webpush)
    sender.notify(Notify("U1", "offered", {"confirm_deadline": 1.79e9}))
    sender._send(*sender._q.get())
    info, payload, kw = sent[0]
    assert info["endpoint"].startswith("https://fcm.googleapis.com/")
    assert payload["title"].endswith("Your turn!") and verify_token("t", payload["token"]) == "U1"
    assert kw["vapid_claims"]["sub"].startswith("mailto:") and kw["ttl"] == 600

    class Gone(Exception):
        pass

    def gone(**kw):
        raise pywebpush.WebPushException("gone", response=type("R", (), {"status_code": 410})())

    monkeypatch.setattr(pywebpush, "webpush", lambda *a, **kw: gone())
    sender.notify(Notify("U1", "time_up", {}))
    sender._send(*sender._q.get())
    assert h.engine.pref("U1", "push") == []


def test_no_push_without_subscriptions_or_keys():
    h = Harness(Config(web_secret="t"))
    sender = PushSender(h.cfg, h.engine, lambda u: "x")
    assert not sender.ready
    sender.notify(Notify("U1", "offered", {}))
    assert sender._q.empty()
