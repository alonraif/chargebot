"""The fair-queue rules: weekly priority, no-jump guarantee, push-down on a missed turn, rush-hour soft cap."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from chargebot.config import Config
from chargebot.domain import LogSession, Session, State, queue_etas, queue_info, week_count, week_start

from .conftest import MIN, Harness

TZ = ZoneInfo("Asia/Jerusalem")


def ts(y, mo, d, h, mi=0) -> float:
    return datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp()


SUNDAY_8 = ts(2026, 11, 15, 8, 0)      # a Sunday morning, inside the rush window
SUNDAY_14 = ts(2026, 11, 15, 14, 0)    # outside it


def make(**kw) -> Harness:
    cfg = Config(web_secret="test", priority_queue=True, timezone="Asia/Jerusalem", **kw)
    h = Harness(cfg)
    h.clock.now = SUNDAY_8
    return h


def set_week(h, **counts):
    h.state.weekly = {"start": week_start(h.cfg, h.clock.now), "counts": dict(counts)}


def charging(h):
    s = h.state.chargers[0].session
    return s.user_id if s else None


def start_charging(h, user):
    h.act("join", user)
    h.act("confirm", user)


def finish(h):
    """Whoever holds the charger confirms and then finishes early."""
    user = charging(h)
    h.act("confirm", user)
    h.act("done", user)
    return user


# -- weekly counter -------------------------------------------------------------------


def test_week_starts_on_sunday_in_local_time():
    cfg = Config(web_secret="test", timezone="Asia/Jerusalem")
    wed = ts(2026, 11, 11, 15, 30)
    assert week_start(cfg, wed) == ts(2026, 11, 8, 0, 0)
    assert week_start(cfg, ts(2026, 11, 8, 0, 1)) == ts(2026, 11, 8, 0, 0)
    assert week_start(cfg, ts(2026, 11, 14, 23, 59)) == ts(2026, 11, 8, 0, 0)  # Saturday night
    assert week_start(cfg, ts(2026, 11, 15, 0, 1)) == ts(2026, 11, 15, 0, 0)   # next Sunday


def test_only_real_charges_count_and_the_week_resets():
    h = make()
    start_charging(h, "A")
    h.act("done", "A")                       # ended early after charging: counts
    assert week_count(h.cfg, h.state, "A", h.clock.now) == 1
    h.act("join", "B")
    h.wait(11)                               # B never confirms: no-show, never counts
    assert week_count(h.cfg, h.state, "B", h.clock.now) == 0
    h.act("join", "C")
    h.act("skip", "C")                       # skipped: doesn't count
    assert week_count(h.cfg, h.state, "C", h.clock.now) == 0
    h.clock.now = ts(2026, 11, 22, 8, 0)     # the following Sunday
    assert week_count(h.cfg, h.state, "A", h.clock.now) == 0


# -- priority ---------------------------------------------------------------------------


def test_fewest_charges_this_week_goes_first():
    h = make()
    set_week(h, D=2, R=1)
    start_charging(h, "X")
    for u in ("D", "R", "M"):                # D joined first, M last
        h.act("join", u)
        h.wait(1)
    assert [e.user_id for e in h.state.queue] == ["M", "R", "D"]
    finish(h)
    assert charging(h) == "M"
    finish(h)
    assert charging(h) == "R"
    finish(h)
    assert charging(h) == "D"


def test_same_tier_keeps_arrival_order_and_heavy_users_can_still_charge():
    h = make()
    set_week(h, D=5)
    h.act("join", "D")                       # nobody else around: no waiting at all
    assert charging(h) == "D"
    start = make()
    set_week(start, A=1, B=1)
    start_charging(start, "X")
    start.act("join", "A")
    start.wait(1)
    start.act("join", "B")
    assert [e.user_id for e in start.state.queue] == ["A", "B"]


def test_priority_is_off_by_default():
    h = Harness(Config(web_secret="test"))
    h.state.weekly = {"start": week_start(h.cfg, h.clock.now), "counts": {"D": 3}}
    start_charging(h, "X")
    h.act("join", "D")
    h.wait(1)
    h.act("join", "M")
    assert [e.user_id for e in h.state.queue] == ["D", "M"]


def test_no_jump_guarantee_after_waiting_long_enough():
    h = make()
    set_week(h, D=2, R=1)
    start_charging(h, "X")
    h.act("join", "D")
    h.wait(10)
    h.act("join", "R")
    h.wait(10)
    h.act("join", "M")
    h.wait(80)                               # D has now waited 100 minutes
    info = {i["user_id"]: i for i in queue_info(h.cfg, h.state, h.clock.now)}
    assert info["D"]["guaranteed"] and info["D"]["position"] == 1
    assert info["D"]["reason"] == "first"
    assert info["M"]["reason"] == "guaranteed"          # D is ahead because of the guarantee
    h.wait(21)                               # X's 2 hours are up; D is offered the charger
    assert charging(h) == "D"


def test_guarantee_time_is_reported_before_it_applies():
    h = make()
    set_week(h, D=2)
    start_charging(h, "X")
    h.act("join", "D")
    info = queue_info(h.cfg, h.state, h.clock.now)[0]
    assert info["guarantee_at"] == h.clock.now + 90 * MIN and not info["guaranteed"]


def test_reason_codes_explain_the_order():
    h = make()
    set_week(h, D=2, R=1)
    start_charging(h, "X")
    for u in ("D", "R", "M"):
        h.act("join", u)
        h.wait(1)
    info = {i["user_id"]: i for i in queue_info(h.cfg, h.state, h.clock.now)}
    assert info["M"]["reason"] == "first" and info["M"]["tier"] == 0
    assert info["R"]["reason"] == "fewer_charges" and [a["user_id"] for a in info["R"]["ahead"]] == ["M"]
    assert info["D"]["reason"] == "fewer_charges" and info["D"]["week_count"] == 2


def test_admin_move_up_pins_above_the_normal_order():
    h = make()
    set_week(h, D=2)
    start_charging(h, "X")
    h.act("join", "M")
    h.wait(1)
    h.act("join", "D")
    assert [e.user_id for e in h.state.queue] == ["M", "D"]
    h.engine.admin_move_up("D")
    assert [e.user_id for e in h.state.queue] == ["D", "M"]
    h.wait(1)
    assert [e.user_id for e in h.state.queue] == ["D", "M"]    # stays put on later ticks


def test_order_is_only_saved_when_it_changes():
    h = make()
    start_charging(h, "X")
    h.act("join", "A")
    v = h.state.version
    h.wait(5)
    assert h.state.version == v              # no churn from re-sorting an unchanged queue


# -- push-down on a missed turn --------------------------------------------------------


def test_missed_turn_steps_back_one_place_and_keeps_the_guarantee_clock():
    h = make(noshow_pushdown=True)
    start_charging(h, "X")
    h.act("join", "A")
    h.wait(1)
    h.act("join", "B")
    h.wait(1)
    h.act("join", "C")
    h.wait(10)
    h.act("done", "X")                       # A is offered the charger
    assert charging(h) == "A"
    joined = h.state.chargers[0].session.joined_at
    h.wait(10)                               # A misses the turn
    assert charging(h) == "B"                # the next person goes first
    assert [e.user_id for e in h.state.queue] == ["A", "C"]
    pushed = h.state.queue[0]
    assert pushed.joined_at == joined and pushed.strikes == 1
    assert [n.kind for n in h.notes("A", "pushed_down")] == ["pushed_down"]


def test_second_missed_turn_drops_the_person():
    h = make(noshow_pushdown=True)
    start_charging(h, "X")
    h.act("join", "A")
    h.wait(1)
    h.act("join", "B")
    h.wait(1)
    h.act("done", "X")
    h.wait(10)                               # A misses; B is offered
    assert charging(h) == "B"
    finish(h)                                # B charges and finishes; A is offered again
    assert charging(h) == "A"
    h.wait(10)                               # A misses again
    assert "A" not in [e.user_id for e in h.state.queue]
    assert charging(h) != "A"


def test_missed_turn_with_nobody_waiting_gets_a_second_chance_then_reclaim():
    h = make(noshow_pushdown=True)
    h.act("join", "A")
    h.wait(10)
    s = h.state.chargers[0].session
    assert s is not None and s.user_id == "A" and s.strikes == 1     # offered again at once
    h.wait(10)
    assert h.state.chargers[0].session is None
    assert h.state.chargers[0].reclaim is not None                    # the old "reclaim" path


def test_missed_turn_still_drops_the_person_when_the_rule_is_off():
    h = make()
    start_charging(h, "X")
    h.act("join", "A")
    h.wait(1)
    h.act("join", "B")
    h.act("done", "X")
    h.wait(10)
    assert "A" not in [e.user_id for e in h.state.queue]


# -- rush-hour soft cap ---------------------------------------------------------------------


def test_cap_cuts_a_rush_charge_to_90_minutes_when_someone_is_waiting():
    h = make(rush_cap_s=90 * MIN)
    start_charging(h, "A")
    h.wait(50)
    h.act("join", "B")
    start = h.state.chargers[0].session.charge_start
    assert queue_etas(h.cfg, h.state, h.clock.now)[0] == start + 90 * MIN   # ETA knows about the cap
    h.wait(40)                                                               # 90 minutes in
    assert charging(h) == "B"                                                # A was cut, B offered
    rec = [e.record for e in h.of(LogSession)][0]
    assert rec["user_id"] == "A" and rec["capped"] is True and rec["outcome"] == "completed"
    warn = h.notes("A", "warn_end")
    assert warn and warn[-1].data["capped"] is True


def test_cap_warns_the_user_before_cutting():
    h = make(rush_cap_s=90 * MIN)
    start_charging(h, "A")
    h.wait(30)
    h.act("join", "B")
    h.wait(55)                                                               # 85 minutes in
    warn = h.notes("A", "warn_end")
    assert warn and warn[-1].data["capped"] and warn[-1].data["can_extend"] is False
    assert charging(h) == "A"


def test_no_cap_when_nobody_is_waiting():
    h = make(rush_cap_s=90 * MIN)
    start_charging(h, "A")
    h.wait(100)
    assert charging(h) == "A"                                                # still has the full slot
    h.wait(21)
    assert charging(h) is None
    assert h.of(LogSession)[0].record["capped"] is False


def test_no_cap_outside_the_rush_window():
    h = make(rush_cap_s=90 * MIN)
    h.clock.now = SUNDAY_14
    start_charging(h, "A")
    h.act("join", "B")
    h.wait(100)
    assert charging(h) == "A"


def test_someone_joining_at_the_last_minute_does_not_cut_a_charge_short():
    h = make(rush_cap_s=90 * MIN)
    start_charging(h, "A")
    h.wait(88)                                                               # too late to warn fairly
    h.act("join", "B")
    h.wait(5)
    assert charging(h) == "A"


def test_cap_is_off_unless_configured():
    h = make()
    start_charging(h, "A")
    h.act("join", "B")
    h.wait(100)
    assert charging(h) == "A"


def test_capped_session_can_be_extended_if_the_queue_empties():
    h = make(rush_cap_s=90 * MIN)
    start_charging(h, "A")
    h.wait(30)
    h.act("join", "B")
    h.wait(55)                                                               # capped, warned
    h.act("leave", "B")
    r = h.act("extend", "A")
    assert r.ok


# -- compatibility -----------------------------------------------------------------------


def test_state_from_a_newer_version_still_loads():
    snap = {
        "chargers": [{"id": "c1", "name": "Charger 1", "future_field": 1, "session": {
            "id": "s1", "user_id": "U1", "charger_id": "c1", "joined_at": 1.0, "offered_at": 1.0,
            "grace_ends_at": 2.0, "confirm_deadline": 3.0, "from_the_future": True}}],
        "queue": [{"user_id": "U2", "joined_at": 1.0, "another_new_field": "x"}],
        "weekly": {"start": 1.0, "counts": {"U1": 2}},
        "version": 3,
    }
    state = State.from_dict(snap)
    assert state.chargers[0].session.user_id == "U1" and state.queue[0].user_id == "U2"
    assert state.weekly["counts"]["U1"] == 2


def test_state_roundtrips_with_the_new_fields():
    h = make(noshow_pushdown=True, rush_cap_s=90 * MIN)
    start_charging(h, "A")
    h.act("join", "B")
    h.act("done", "A")
    restored = State.from_dict(h.state.to_dict())
    assert restored.to_dict() == h.state.to_dict()


def test_old_snapshot_without_new_fields_loads():
    old = {"chargers": [{"id": "c1", "name": "Charger 1"}], "queue": [{"user_id": "U9", "joined_at": 5.0}], "version": 1}
    state = State.from_dict(old)
    assert state.queue[0].strikes == 0 and not state.queue[0].pushed and state.weekly == {}


# -- rush slot: reserve 90, extend to 120 if nobody is waiting 10 minutes before the end -------------


def test_rush_charge_reserves_90_minutes_from_the_start():
    h = make(rush_cap_s=90 * MIN)
    start_charging(h, "A")
    s = h.state.chargers[0].session
    assert s.rush and s.ends_at - s.charge_start == 90 * MIN
    note = h.notes("A", "charging")[-1]
    assert note.data["rush"] and note.data["full_ends_at"] == s.charge_start + 120 * MIN


def test_rush_slot_extends_to_the_full_slot_when_nobody_is_waiting():
    h = make(rush_cap_s=90 * MIN)
    start_charging(h, "A")
    h.wait(80)
    s = h.state.chargers[0].session
    assert s.auto_extended and s.ends_at - s.charge_start == 120 * MIN
    assert [n.kind for n in h.notes("A", "rush_extended")] == ["rush_extended"]
    assert not h.notes("A", "warn_end")                          # no "ending soon" at 80 minutes
    h.wait(30)                                                   # normal warning before the new end
    assert h.notes("A", "warn_end") and not h.notes("A", "warn_end")[-1].data["capped"]
    h.wait(11)
    rec = h.of(LogSession)[0].record
    assert rec["rush"] and rec["auto_extended"] and not rec["capped"]


def test_rush_slot_ends_at_90_when_someone_is_waiting_at_the_check():
    h = make(rush_cap_s=90 * MIN)
    start_charging(h, "A")
    h.wait(60)
    h.act("join", "B")
    h.wait(20)                                                   # the check at 80 minutes
    s = h.state.chargers[0].session
    assert s.capped and not s.auto_extended and s.ends_at - s.charge_start == 90 * MIN
    assert h.notes("A", "warn_end")[-1].data["capped"]
    h.wait(10)
    assert charging(h) == "B"


def test_someone_joining_after_the_extension_does_not_shorten_it():
    h = make(rush_cap_s=90 * MIN)
    start_charging(h, "A")
    h.wait(82)                                                   # extended at 80
    h.act("join", "B")
    h.wait(20)
    assert charging(h) == "A"


def test_a_restart_during_the_check_never_cuts_short_an_empty_line():
    h = make(rush_cap_s=90 * MIN)
    start_charging(h, "A")
    h.wait(70)
    h.clock.advance(25)                                          # the bot was down from 70 to 95 minutes
    h.engine.tick()
    s = h.state.chargers[0].session
    assert s is not None and s.user_id == "A" and s.auto_extended


def test_unconfirmed_session_in_rush_hours_gets_the_rush_slot_too():
    h = make(rush_cap_s=90 * MIN)
    h.act("join", "A")
    h.wait(6)                                                    # grace over: clock starts without a confirm
    s = h.state.chargers[0].session
    assert s.rush and s.ends_at - s.charge_start == 90 * MIN


def test_outside_rush_hours_the_slot_is_the_full_two_hours():
    h = make(rush_cap_s=90 * MIN)
    h.clock.now = SUNDAY_14
    start_charging(h, "A")
    s = h.state.chargers[0].session
    assert not s.rush and s.ends_at - s.charge_start == 120 * MIN


def test_manual_extension_settles_the_rush_check():
    h = make(rush_cap_s=90 * MIN)
    start_charging(h, "A")
    h.wait(79)
    h.clock.advance(1)                                           # minute 80, before the scheduler's tick
    assert h.act("extend", "A").ok                               # tapped Extend the moment it opened
    s = h.state.chargers[0].session
    h.wait(1)
    assert s.rush_checked and not s.auto_extended


# -- the rush slot follows registration time, not charge time ----------------------------------------


def test_morning_registration_keeps_the_rush_slot_even_when_charging_in_the_afternoon():
    h = make(rush_cap_s=90 * MIN)
    h.clock.now = ts(2026, 11, 15, 12, 50)
    start_charging(h, "X")                                       # X registered 12:50: rush slot
    h.act("join", "A")                                           # A registered 12:50 too
    h.clock.now = ts(2026, 11, 15, 14, 10)
    h.act("done", "X")
    h.act("confirm", "A")                                        # A starts at 14:10, outside the window
    s = h.state.chargers[0].session
    assert s.user_id == "A" and s.rush and s.ends_at - s.charge_start == 90 * MIN


def test_afternoon_registration_gets_the_full_slot():
    h = make(rush_cap_s=90 * MIN)
    h.clock.now = ts(2026, 11, 15, 13, 5)
    start_charging(h, "A")
    s = h.state.chargers[0].session
    assert not s.rush and s.ends_at - s.charge_start == 120 * MIN


def test_registering_before_seven_is_no_loophole():
    h = make(rush_cap_s=90 * MIN)
    h.clock.now = ts(2026, 11, 15, 6, 30)
    start_charging(h, "A")
    assert h.state.chargers[0].session.rush


def test_etas_use_each_persons_registration_time():
    h = make(rush_cap_s=90 * MIN)
    h.clock.now = ts(2026, 11, 15, 12, 0)
    start_charging(h, "X")                                       # rush slot: ends 13:30
    h.act("join", "A")                                           # registered 12:00: will get 90
    h.clock.now = ts(2026, 11, 15, 13, 10)
    h.act("join", "B")                                           # registered 13:10: will get 120
    h.clock.now = ts(2026, 11, 15, 13, 11)
    h.act("join", "C")
    etas = queue_etas(h.cfg, h.state, h.clock.now)
    end_x = ts(2026, 11, 15, 13, 30)
    assert etas[0] == end_x
    assert etas[1] == end_x + h.cfg.grace_s + 90 * MIN          # A's rush slot
    assert etas[2] == etas[1] + h.cfg.grace_s + 120 * MIN        # B's full slot


def test_a_pushed_down_person_keeps_their_morning_registration():
    h = make(rush_cap_s=90 * MIN, noshow_pushdown=True)
    start_charging(h, "X")
    h.act("join", "A")
    h.wait(1)
    h.act("join", "B")
    h.clock.now = ts(2026, 11, 15, 13, 30)
    h.act("done", "X")
    h.wait(10)                                                   # A misses; B charges; A is back in line
    finish(h)
    h.act("confirm", "A")
    assert h.state.chargers[0].session.rush                       # still the 08:00 registration
