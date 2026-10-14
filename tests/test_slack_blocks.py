from __future__ import annotations

import json

import pytest

from chargebot.config import Config
from chargebot.slack import blocks as B

from .conftest import Harness

KINDS = ["offered", "confirm_alert", "maybe_soon", "lost_turn", "charging", "warn_end", "time_up", "nudge",
         "next_up_soon", "kicked", "removed"]

DATA = {
    "charger_id": "c1", "grace_ends_at": 1.7e9, "confirm_deadline": 1.7e9, "previous_user_id": "U0",
    "final": True, "clock_started": True, "minutes": 2, "reclaimable": True, "ends_at": 1.7e9,
    "can_extend": True, "next_user_id": "U2", "from_user_id": "U2", "eta": 1.7e9, "by": "UA", "user_id": "U1",
}


def names(uid):
    return {"U0": "Ari", "U1": "Noa", "U2": "Dana", "UA": "Admin"}.get(uid, "Someone")


def all_buttons(blocks):
    for b in blocks:
        for el in b.get("elements", []):
            if el.get("type") == "button":
                yield el
        acc = b.get("accessory")
        if acc and acc.get("type") == "button":
            yield acc


@pytest.mark.parametrize("kind", KINDS)
def test_every_notification_renders(kind):
    h = Harness(Config(web_secret="t"))
    text, blocks = B.notification(kind, DATA, h.cfg, h.state, names, link="https://x/?t=1")
    assert text and blocks
    json.dumps(blocks)
    assert len(blocks) <= 50
    for btn in all_buttons(blocks):
        assert len(btn["text"]["text"]) <= 75
        assert btn["action_id"] in B.USER_ACTION_IDS or btn["action_id"] == B.A_LINK


def test_single_charger_messages_never_name_the_charger():
    h = Harness(Config(web_secret="t"))
    for kind in KINDS:
        text, blocks = B.notification(kind, DATA, h.cfg, h.state, names)
        assert "Charger 1" not in json.dumps(blocks)


def test_multi_charger_messages_name_the_charger():
    h = Harness(Config(web_secret="t", chargers=("North", "South")))
    text, blocks = B.notification("offered", dict(DATA, charger_id="c2"), h.cfg, h.state, names)
    assert "South" in json.dumps(blocks)


def test_offer_has_nudge_only_with_previous_user():
    h = Harness(Config(web_secret="t"))
    _, with_prev = B.notification("offered", DATA, h.cfg, h.state, names)
    _, without = B.notification("offered", dict(DATA, previous_user_id=None), h.cfg, h.state, names)
    assert any(b["action_id"] == B.A_NUDGE for b in all_buttons(with_prev))
    assert not any(b["action_id"] == B.A_NUDGE for b in all_buttons(without))


def test_every_reply_code_has_copy():
    h = Harness(Config(web_secret="t"))
    codes = ["offered", "queued", "already_active", "already_queued", "confirmed", "already_confirmed",
             "not_offered", "skipped", "done", "unplugged", "nothing_to_end", "nothing_to_unplug", "left",
             "not_queued", "extended", "queue_waiting", "too_early", "not_charging", "nudged",
             "nudge_too_soon", "nothing_to_nudge", "reclaimed", "reclaim_expired", "ended", "no_session",
             "removed", "moved", "already_first", "out_of_order", "back_in_service", "pref_saved", "not_admin"]
    for code in codes:
        assert B.reply(code, {}, h.cfg, h.state, names) != code


def test_status_home_admin_and_live_views_render():
    h = Harness(Config(web_secret="t", admin_user_ids=frozenset({"UA"})))
    h.act("join", "U1")
    h.act("confirm", "U1")
    h.act("join", "U2")
    state, now = h.engine.view()
    for viewer in ("U1", "U2", "U3", "UA"):
        B.status_blocks(state, now, h.cfg, names, viewer)
        home = B.home_view(state, now, h.cfg, names, viewer, None, [])
        assert home["type"] == "home" and len(home["blocks"]) <= 100
    assert any(b.get("accessory", {}).get("action_id") == B.A_ADMIN_OPEN
               for b in B.home_view(state, now, h.cfg, names, "UA", None, [])["blocks"])
    admin = B.admin_modal(state, now, h.cfg, names)
    assert admin["type"] == "modal"
    text, live = B.live_message(state, now, h.cfg, names, "https://x")
    assert "waiting" in text
    json.dumps([admin, live])


def test_viewer_buttons_match_situation():
    h = Harness(Config(web_secret="t"))
    h.act("join", "U1")
    h.act("join", "U2")
    state, now = h.engine.view()
    ids = lambda u: [b["action_id"] for b in B.viewer_buttons(state, h.cfg, u, now)["elements"]]  # noqa: E731
    assert ids("U1")[:2] == [B.A_CONFIRM, B.A_SKIP]
    assert ids("U2") == [B.A_LEAVE]
    assert ids("U3") == [B.A_JOIN]


def test_extend_button_only_in_last_minutes():
    h = Harness(Config(web_secret="t"))
    h.act("join", "U1")
    h.act("confirm", "U1")

    def ids():
        state, now = h.engine.view()
        return [b["action_id"] for b in B.viewer_buttons(state, h.cfg, "U1", now)["elements"]]

    assert B.A_EXTEND not in ids()
    h.wait(111)
    assert B.A_EXTEND in ids()


def test_closed_messages_have_text_for_every_reason():
    for reason in B.CLOSE_TEXT:
        text, blocks = B.closed(reason, {"at": 1.7e9, "ends_at": 1.7e9})
        assert "{" not in text.replace("{time}", "")
