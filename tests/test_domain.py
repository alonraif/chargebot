from __future__ import annotations

from chargebot.config import Config
from chargebot.domain import CancelInvite, CloseMessages, LogSession, SendInvite, State, queue_etas

from .conftest import MIN, Harness


def outcomes(h):
    return [e.record["outcome"] for e in h.of(LogSession)]


def test_join_free_charger_offers_it(h):
    r = h.act("join", "U1")
    assert r.ok and r.code == "offered"
    assert h.state.chargers[0].session.user_id == "U1"
    assert [n.kind for n in h.notes("U1")] == ["offered"]


def test_second_user_queues_with_eta(h):
    h.act("join", "U1")
    r = h.act("join", "U2")
    assert r.code == "queued" and r.data["position"] == 1
    # U1 may take up to grace + charge before the charger frees up.
    assert r.data["eta"] == h.clock.now + h.cfg.grace_s + h.cfg.charge_s


def test_duplicate_joins_are_rejected(h):
    h.act("join", "U1")
    h.act("join", "U2")
    assert h.act("join", "U1").code == "already_active"
    assert h.act("join", "U2").code == "already_queued"


def test_confirm_before_grace_starts_clock_at_confirm(h):
    h.act("join", "U1")
    h.wait(3)
    r = h.act("confirm", "U1")
    s = h.state.chargers[0].session
    assert r.code == "confirmed"
    assert s.charge_start == h.clock.now
    assert s.ends_at == h.clock.now + h.cfg.charge_s
    assert h.notes("U1", "charging")
    assert len(h.of(SendInvite)) == 1


def test_confirm_after_grace_keeps_clock_from_grace_end(h):
    h.act("join", "U1")
    offered = h.clock.now
    h.wait(7)
    h.act("confirm", "U1")
    s = h.state.chargers[0].session
    assert s.charge_start == offered + h.cfg.grace_s
    assert s.ends_at == offered + h.cfg.grace_s + h.cfg.charge_s


def test_confirm_alerts_then_no_show_with_queue(h):
    h.act("join", "U1")
    h.act("join", "U2")
    h.clear()
    h.wait(5)
    alerts = h.notes("U1", "confirm_alert")
    assert len(alerts) == 1 and not alerts[0].data["final"] and alerts[0].data["clock_started"]
    h.wait(3)
    alerts = h.notes("U1", "confirm_alert")
    assert len(alerts) == 2 and alerts[1].data["final"]
    assert h.notes("U2", "maybe_soon")
    h.wait(2)
    lost = h.notes("U1", "lost_turn")
    assert len(lost) == 1 and lost[0].data["reclaimable"] is False
    assert outcomes(h) == ["no_show"]
    assert h.state.chargers[0].session.user_id == "U2"
    assert h.notes("U2", "offered")


def test_no_show_without_queue_releases_and_can_be_reclaimed(h):
    h.act("join", "U1")
    h.wait(10)
    charger = h.state.chargers[0]
    assert charger.session is None and charger.reclaim.user_id == "U1"
    assert h.notes("U1", "lost_turn")[0].data["reclaimable"] is True
    r = h.act("reclaim", "U1")
    assert r.code == "reclaimed"
    assert charger.session.confirmed and charger.session.user_id == "U1"


def test_reclaim_expires_once_someone_else_takes_the_charger(h):
    h.act("join", "U1")
    h.wait(10)
    h.act("join", "U2")
    assert h.act("reclaim", "U1").code == "reclaim_expired"


def test_full_session_warns_and_ends_then_promotes(h):
    h.act("join", "U1")
    h.act("confirm", "U1")
    h.act("join", "U2")
    h.clear()
    h.wait(110)
    warn = h.notes("U1", "warn_end")
    assert len(warn) == 1 and warn[0].data["can_extend"] is False
    h.wait(10)
    assert h.notes("U1", "time_up")[0].data["next_user_id"] == "U2"
    assert outcomes(h) == ["completed"]
    charger = h.state.chargers[0]
    assert charger.unplug.user_id == "U1"
    assert charger.session.user_id == "U2"
    # A completed session keeps its calendar reminder.
    assert not h.of(CancelInvite)


def test_extend_only_when_nobody_waits(h):
    h.act("join", "U1")
    h.act("confirm", "U1")
    h.wait(111)
    assert h.notes("U1", "warn_end")[0].data["can_extend"] is True
    end_before = h.state.chargers[0].session.ends_at
    r = h.act("extend", "U1")
    assert r.code == "extended"
    assert h.state.chargers[0].session.ends_at == end_before + h.cfg.extend_s
    invites = h.of(SendInvite)
    assert invites[-1].sequence == 1
    h.act("join", "U2")
    assert h.act("extend", "U1").code == "queue_waiting"


def test_extend_limit(h):
    h.act("join", "U1")
    h.act("confirm", "U1")
    assert h.act("extend", "U1").ok
    assert h.act("extend", "U1").ok
    assert h.act("extend", "U1").code == "max_extensions"


def test_done_early_cancels_invite_and_promotes(h):
    h.act("join", "U1")
    h.act("confirm", "U1")
    h.act("join", "U2")
    r = h.act("done", "U1")
    assert r.code == "done"
    assert len(h.of(CancelInvite)) == 1
    assert outcomes(h) == ["ended_early"]
    assert h.state.chargers[0].session.user_id == "U2"
    # "Done" means unplugged, so nobody is asked to move their car.
    assert h.state.chargers[0].unplug is None


def test_skip_passes_turn(h):
    h.act("join", "U1")
    h.act("join", "U2")
    assert h.act("skip", "U1").code == "skipped"
    assert h.state.chargers[0].session.user_id == "U2"
    assert outcomes(h) == ["skipped"]


def test_leave_queue(h):
    h.act("join", "U1")
    h.act("join", "U2")
    assert h.act("leave", "U2").code == "left"
    assert h.act("leave", "U2").code == "not_queued"


def test_nudge_previous_user_with_cooldown(h):
    h.act("join", "U1")
    h.act("confirm", "U1")
    h.act("join", "U2")
    h.wait(120)
    assert h.act("nudge", "U2").code == "nudged"
    assert h.notes("U1", "nudge")
    assert h.act("nudge", "U2").code == "nudge_too_soon"
    h.wait(5)
    assert h.act("nudge", "U2").code == "nudged"


def test_confirm_by_next_user_hands_over_and_closes_unplug(h):
    h.act("join", "U1")
    h.act("confirm", "U1")
    h.act("join", "U2")
    h.wait(120)
    h.engine.attach_message("unplug:c1", "D1", "111.1")
    h.clear()
    h.act("confirm", "U2")
    assert h.state.chargers[0].unplug is None
    closes = h.of(CloseMessages)
    assert any(c.reason == "handed_over" for c in closes)


def test_unplugged_clears_reminder(h):
    h.act("join", "U1")
    h.act("confirm", "U1")
    h.wait(120)
    assert h.act("unplugged", "U1").code == "unplugged"
    assert h.state.chargers[0].unplug is None


def test_tracked_messages_closed_on_confirm(h):
    h.act("join", "U1")
    sid = h.state.chargers[0].session.id
    h.engine.attach_message(sid, "D1", "1.1")
    h.clear()
    h.act("confirm", "U1")
    closes = h.of(CloseMessages)
    assert closes and closes[0].reason == "confirmed" and closes[0].refs == [{"channel": "D1", "ts": "1.1"}]


def test_heads_up_once_when_close_to_turn(h):
    h.act("join", "U1")
    h.act("confirm", "U1")
    h.act("join", "U2")
    h.wait(100)
    assert not h.notes("U2", "next_up_soon")
    h.wait(6)
    assert len(h.notes("U2", "next_up_soon")) == 1
    h.wait(5)
    assert len(h.notes("U2", "next_up_soon")) == 1


def test_restart_resumes_and_fires_missed_deadline_once():
    h = Harness(Config(web_secret="t"))
    h.act("join", "U1")
    h.act("confirm", "U1")
    snapshot = h.store.snapshot
    # New process, an hour of downtime past the end of the session.
    h2 = Harness(Config(web_secret="t"))
    h2.clock.now = h.clock.now + 125 * MIN
    h2.store.snapshot = snapshot
    h2.engine.load()
    h2.engine.tick()
    h2.engine.tick()
    assert len(h2.notes("U1", "time_up")) == 1
    assert not h2.notes("U1", "warn_end")


def test_restart_overdue_alerts_send_only_latest():
    h = Harness(Config(web_secret="t"))
    h.act("join", "U1")
    h.act("join", "U2")
    h.clock.advance(9)
    h.engine.tick()
    alerts = h.notes("U1", "confirm_alert")
    assert len(alerts) == 1 and alerts[0].data["final"]


def test_out_of_order_blocks_offers_and_resume_promotes(h):
    h.engine.set_out_of_order("c1", "Broken cable")
    assert h.act("join", "U1").code == "queued"
    h.wait(1)
    assert h.state.chargers[0].session is None
    h.engine.set_out_of_order("c1", None)
    assert h.state.chargers[0].session.user_id == "U1"


def test_admin_end_and_remove(h):
    h.act("join", "U1")
    h.act("join", "U2")
    h.act("join", "U3")
    assert h.engine.admin_remove("U3", by="A").code == "removed"
    assert h.notes("U3", "removed")
    assert h.engine.admin_end("c1", by="A").code == "ended"
    assert h.notes("U1", "kicked")
    assert h.state.chargers[0].session.user_id == "U2"


def test_admin_move_up(h):
    h.act("join", "U1")
    for u in ("U2", "U3"):
        h.act("join", u)
    h.engine.admin_move_up("U3")
    assert [q.user_id for q in h.state.queue] == ["U3", "U2"]


def test_email_pref_off_skips_invite(h):
    h.engine.set_pref("U1", "email_reminder", False)
    h.act("join", "U1")
    h.act("confirm", "U1")
    assert not h.of(SendInvite)


def test_two_chargers_share_one_queue(h2):
    h2.act("join", "U1")
    h2.act("join", "U2")
    assert {c.session.user_id for c in h2.state.chargers} == {"U1", "U2"}
    assert h2.act("join", "U3").code == "queued"
    h2.act("confirm", "U2")
    h2.act("done", "U2")
    assert h2.state.charger("c2").session.user_id == "U3"


def test_queue_etas_pick_earliest_charger(h2):
    h2.act("join", "U1")
    h2.act("confirm", "U1")
    h2.wait(60)
    h2.act("join", "U2")
    h2.act("confirm", "U2")
    h2.act("join", "U3")
    h2.act("join", "U4")
    etas = queue_etas(h2.cfg, h2.state, h2.clock.now)
    c1_end = h2.state.charger("c1").session.ends_at
    c2_end = h2.state.charger("c2").session.ends_at
    assert etas == [c1_end, c2_end]


def test_adding_a_charger_via_config_keeps_state():
    h = Harness(Config(web_secret="t"))
    h.act("join", "U1")
    h.act("join", "U2")
    snapshot = h.store.snapshot
    two = Harness(Config(web_secret="t", chargers=("Charger 1", "Charger 2")))
    two.store.snapshot = snapshot
    two.engine.load()
    assert [c.id for c in two.state.chargers] == ["c1", "c2"]
    two.engine.tick()
    assert two.state.charger("c2").session.user_id == "U2"


def test_state_round_trips_through_dict(h):
    h.act("join", "U1")
    h.act("confirm", "U1")
    h.act("join", "U2")
    again = State.from_dict(h.state.to_dict())
    assert again.to_dict() == h.state.to_dict()


def test_rule_exception_rolls_back_state(h):
    h.act("join", "U1")
    before = h.state.to_dict()

    def boom(state, now):
        state.queue.clear()
        state.chargers[0].session = None
        raise RuntimeError("bad")

    try:
        h.engine.apply(boom)
    except RuntimeError:
        pass
    assert h.state.to_dict() == before
