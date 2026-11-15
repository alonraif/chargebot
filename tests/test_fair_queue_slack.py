"""Slack wording for the fair-queue rules: reasons, weekly count, new notifications, help."""

from __future__ import annotations

import json

import pytest

from chargebot import push
from chargebot.config import Config
from chargebot.domain import week_start
from chargebot.slack import blocks as B

from .conftest import Harness
from .test_fair_queue import SUNDAY_8, charging, make, set_week, start_charging


def names(uid):
    return {"X": "Xavi", "D": "Dan", "R": "Ron", "M": "Mia", "A": "Ari"}.get(uid, uid)


def text_of(blocks):
    return json.dumps(blocks, ensure_ascii=False)


def queue(h, viewer=None):
    return B.queue_lines(h.state, h.clock.now, h.cfg, names, viewer)


def busy_queue():
    """X is charging; D (2 charges this week), R (1) and M (0) are waiting."""
    h = make()
    set_week(h, D=2, R=1)
    start_charging(h, "X")
    for u in ("D", "R", "M"):
        h.act("join", u)
        h.wait(1)
    return h


# -- queue rows ---------------------------------------------------------------------------


def test_queue_rows_are_unchanged_when_the_rules_are_off():
    h = Harness(Config(web_secret="t"))
    h.clock.now = SUNDAY_8
    start_charging(h, "X")
    h.act("join", "D")
    h.wait(1)
    h.act("join", "M")
    out = B.queue_lines(h.state, h.clock.now, h.cfg, names)
    assert "⭐" not in out and "🛡" not in out and "↩️" not in out
    assert out.splitlines()[1].startswith("1. Dan") and out.splitlines()[2].startswith("2. Mia")


def test_queue_rows_show_order_and_a_public_tag_without_counts():
    h = busy_queue()
    rows = queue(h).splitlines()
    assert rows[1].startswith("1. Mia") and "⭐ first charge this week" in rows[1]
    assert rows[2].startswith("2. Ron") and rows[3].startswith("3. Dan")
    assert "×" not in queue(h)                      # exact counts stay private


def test_protected_and_second_chance_tags():
    h = busy_queue()
    h.wait(95)                                      # Dan has waited long enough
    assert "🛡 protected" in queue(h).splitlines()[1]
    h2 = make(noshow_pushdown=True)
    start_charging(h2, "X")
    h2.act("join", "A")
    h2.wait(1)
    h2.act("join", "M")
    h2.act("done", "X")
    h2.wait(10)                                     # A misses the turn; M gets it
    assert "↩️ second chance" in queue(h2)


# -- personal explanation and weekly count ---------------------------------------------------


def test_viewer_sees_why_and_how_many_times_they_charged():
    h = busy_queue()
    out = text_of(B.status_blocks(h.state, h.clock.now, h.cfg, names, "D"))
    assert "Ahead of you: 2 people with fewer charges this week" in out
    assert "You've charged *2×* this week" in out
    assert "Nobody can jump ahead of you after" in out        # a heavier user is told when they're protected


def test_first_in_line_and_light_user_text():
    h = busy_queue()
    out = text_of(B.status_blocks(h.state, h.clock.now, h.cfg, names, "M"))
    assert "You're next in line." in out and "You've charged *0×* this week" in out
    assert "Nobody can jump ahead" not in out                  # nothing to warn a first-timer about


def test_guaranteed_viewer_is_told_nobody_can_overtake():
    h = busy_queue()
    h.wait(95)
    out = text_of(B.status_blocks(h.state, h.clock.now, h.cfg, names, "D"))
    assert "nobody can go ahead of you now" in out


def test_weekly_count_shows_even_when_not_in_line():
    h = make()
    set_week(h, A=3)
    out = text_of(B.status_blocks(h.state, h.clock.now, h.cfg, names, "A"))
    assert "You've charged *3×* this week" in out


def test_home_view_shows_weekly_count_and_reason():
    h = busy_queue()
    view = B.home_view(h.state, h.clock.now, h.cfg, names, "R", None, [])
    out = text_of(view["blocks"])
    assert "You've charged *1×* this week" in out and "Ahead of you: 1 person with fewer charges" in out


def test_nothing_new_appears_when_the_rules_are_off():
    h = Harness(Config(web_secret="t"))
    h.clock.now = SUNDAY_8
    start_charging(h, "X")
    h.act("join", "D")
    out = text_of(B.status_blocks(h.state, h.clock.now, h.cfg, names, "D"))
    assert "this week" not in out and "Ahead of you" not in out


def test_join_replies_explain_the_place():
    h = busy_queue()
    r = h.act("join", "A")                      # Ari has no charges, so goes behind Mia (same tier, joined earlier)
    assert r.code == "queued"
    reply = B.reply(r.code, r.data, h.cfg, h.state, names)
    assert "#2" in reply and "Ahead of you: 1 person who joined before you." in reply
    again = h.act("join", "A")
    assert "Ahead of you: 1 person who joined before you." in B.reply(again.code, again.data, h.cfg, h.state, names)


def test_reply_is_plain_when_the_rules_are_off():
    h = Harness(Config(web_secret="t"))
    start_charging(h, "X")
    r = h.act("join", "D")
    assert B.reply(r.code, r.data, h.cfg, h.state, names).count("\n") == 0


# -- notifications -----------------------------------------------------------------------------


def test_pushed_down_notifications_render():
    h = make(noshow_pushdown=True)
    for data in ({"position": 2, "second_chance": False}, {"position": 1, "second_chance": True}):
        text, blocks = B.notification("pushed_down", data, h.cfg, h.state, names)
        assert "missed your turn" in text and blocks
    text, blocks = B.notification("pushed_down", {"position": 3, "second_chance": False}, h.cfg, h.state, names)
    assert "#3" in text_of(blocks) and any(b["text"]["text"] == "Leave the queue" for b in
                                           [e for blk in blocks for e in blk.get("elements", [])])


def test_capped_warning_names_the_rush_hour_limit():
    h = make(rush_cap_s=90 * 60)
    text, blocks = B.notification("warn_end", {"ends_at": 1.7e9, "capped": True, "can_extend": False},
                                  h.cfg, h.state, names)
    out = text_of(blocks)
    assert "Rush-hour limit" in text and "07:00" in out and "13:00" in out and "90 min" in out
    assert "Extend" not in out


def test_plain_warning_is_unchanged_when_not_capped():
    h = make()
    text, _ = B.notification("warn_end", {"ends_at": 1.7e9, "can_extend": True}, h.cfg, h.state, names)
    assert "left" in text and "Rush-hour" not in text


def test_time_up_mentions_the_limit_when_capped():
    h = make(rush_cap_s=90 * 60)
    _, blocks = B.notification("time_up", {"next_user_id": "M", "capped": True}, h.cfg, h.state, names)
    assert "rush-hour limit" in text_of(blocks)


def test_offered_notification_mentions_rush_rule_only_inside_rush_hours(monkeypatch):
    h = make(rush_cap_s=90 * 60)
    data = {"confirm_deadline": 1.7e9, "grace_ends_at": 1.7e9}
    monkeypatch.setattr(B.time, "time", lambda: SUNDAY_8)
    _, inside = B.notification("offered", data, h.cfg, h.state, names)
    monkeypatch.setattr(B.time, "time", lambda: SUNDAY_8 + 8 * 3600)      # 16:00
    _, outside = B.notification("offered", data, h.cfg, h.state, names)
    assert "Rush hours" in text_of(inside) and "Rush hours" not in text_of(outside)


# -- help, admin, push ------------------------------------------------------------------------------


def test_help_describes_only_the_rules_that_are_on():
    plain = B.help_text(Config(web_secret="t"))
    assert "Fair line" not in plain and "Rush hours" not in plain
    full = B.help_text(Config(web_secret="t", priority_queue=True, rush_cap_s=90 * 60, noshow_pushdown=True))
    assert "Fair line" in full and "Rush hours" in full and "Missed your turn" in full and "90 min" in full


def test_admin_queue_shows_counts_and_pins():
    h = busy_queue()
    h.engine.admin_move_up("D")
    modal = B.admin_modal(h.engine.state, h.clock.now, h.cfg, names)
    out = text_of(modal["blocks"])
    assert "Dan · 2× this week · 📌 pinned" in out


def test_push_messages_for_the_new_notifications():
    cfg = Config(web_secret="t", rush_cap_s=90 * 60)
    down = push.message("pushed_down", {"position": 2, "second_chance": False}, cfg, names)
    again = push.message("pushed_down", {"second_chance": True}, cfg, names)
    capped = push.message("warn_end", {"ends_at": SUNDAY_8, "capped": True}, cfg, names)
    plain = push.message("warn_end", {"ends_at": SUNDAY_8, "can_extend": True}, cfg, names)
    assert "#2" in down["body"] and "one more chance" in again["body"]
    assert capped["title"] == "⏳ Rush-hour limit" and plain["title"] == "⏳ 10 minutes left"
