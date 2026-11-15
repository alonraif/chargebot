"""The dashboard JSON for the fair-queue rules: tags for everyone, counts and reasons only for the viewer."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import pytest

from chargebot.config import Config
from chargebot.domain import week_start
from chargebot.slack.users import Profile
from chargebot.web.server import WebApp, make_handler
from chargebot.web.tokens import make_token

from .test_fair_queue import MIN, make, set_week, start_charging
from .test_web import SECRET, get

STATIC = Path(__file__).resolve().parent.parent / "chargebot" / "web" / "static"


def profile(uid):
    return Profile(uid, {"X": "Xavi", "D": "Dan", "R": "Ron", "M": "Mia"}.get(uid, uid))


@pytest.fixture
def fair():
    from http.server import ThreadingHTTPServer
    h = make(rush_cap_s=90 * MIN, noshow_pushdown=True)
    h.cfg_secret = SECRET
    object.__setattr__(h.cfg, "web_secret", SECRET)
    set_week(h, D=2, R=1)
    start_charging(h, "X")
    for u in ("D", "R", "M"):
        h.act("join", u)
        h.wait(1)
    app = WebApp(h.cfg, h.engine, profile)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield h, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def state(base, user=None):
    url = base + "/api/state" + (f"?t={make_token(SECRET, user)}" if user else "")
    return json.loads(get(url)[1])


def test_rules_are_published_for_the_pages(fair):
    h, base = fair
    r = state(base)["rules"]
    assert r == {"priority": True, "pushdown": True, "nojump_s": 90 * MIN, "rush_cap_s": 90 * MIN,
                 "rush_start": 7, "rush_end": 13, "charge_s": 120 * MIN}


def test_queue_is_in_offer_order_with_public_tags_only(fair):
    h, base = fair
    data = state(base)
    assert [q["user"]["name"] for q in data["queue"]] == ["Mia", "Ron", "Dan"]
    assert data["queue"][0]["tags"] == {"protected": False, "second_chance": False, "priority": True}
    assert data["queue"][1]["tags"]["priority"] is False
    text = json.dumps(data)
    assert "week" not in text and "week_count" not in text          # no exact counts for anonymous viewers
    assert '"user_id"' not in text and '"ahead"' not in text            # no user ids or per-person reasons either
    assert data["me"] is None


def test_viewer_gets_their_count_and_reason_but_others_do_not(fair):
    h, base = fair
    me = state(base, "D")["me"]
    assert me["week"] == {"count": 2}
    assert me["queue"]["position"] == 3 and me["queue"]["reason"] == "fewer_charges"
    assert me["queue"]["ahead"] == {"fewer_charges": 2} and me["queue"]["tier"] == 2
    assert me["queue"]["guarantee_at"] and not me["queue"]["guaranteed"]
    other = state(base, "M")["me"]
    assert other["week"] == {"count": 0} and other["queue"]["position"] == 1 and other["queue"]["ahead"] == {}
    assert json.dumps(state(base, "M")["queue"]).count("count") == 0


def test_guarantee_shows_up_after_the_wait(fair):
    h, base = fair
    h.wait(95)                           # X was cut at 90 min; Dan was offered the charger; Ron and Mia still wait
    me = state(base, "R")["me"]["queue"]
    assert me["guaranteed"] and me["guarantee_at"] is None and me["position"] == 1
    assert state(base)["queue"][0]["tags"]["protected"] is True


def test_charging_card_says_when_a_session_was_capped(fair):
    h, base = fair
    assert state(base)["chargers"][0]["capped"] is False
    h.wait(85)                                                          # warned and capped at ~90 minutes
    assert state(base)["chargers"][0]["capped"] is True


def test_week_is_null_and_rules_are_off_by_default(server=None):
    from http.server import ThreadingHTTPServer
    from .conftest import Harness
    h = Harness(Config(web_secret=SECRET))
    h.act("join", "U1")
    h.act("join", "U2")
    app = WebApp(h.cfg, h.engine, profile)
    data = app.state_json("U2")
    assert data["rules"]["priority"] is False and data["me"]["week"] is None
    assert data["queue"][0]["tags"] == {"protected": False, "second_chance": False, "priority": False}


def test_a_missed_turn_shows_a_second_chance_tag(fair):
    h, base = fair
    h.act("done", "X")                                                  # Mia is offered the charger
    h.wait(10)                                                          # Mia misses it; Ron is offered it
    queue = state(base)["queue"]
    assert queue[0]["user"]["name"] == "Mia" or any(q["user"]["name"] == "Mia" for q in queue)
    mia = next(q for q in queue if q["user"]["name"] == "Mia")
    assert mia["tags"]["second_chance"] is True


def test_static_pages_are_cache_busted_and_the_guide_has_the_new_section():
    for page in ("index.html", "kiosk.html", "guide.html", "stats.html"):
        text = (STATIC / page).read_text()
        assert "?v=10" not in text, page
    assert "juice-v11" in (STATIC / "sw.js").read_text()
    guide = (STATIC / "guide.html").read_text()
    assert 'id="fair"' in guide and "hidden" in re.search(r'<section[^>]*id="fair"[^>]*>', guide).group(0)
    for rule in ("priority", "pushdown", "rush"):
        assert f'data-rule="{rule}"' in guide
