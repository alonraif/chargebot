"""Run the dashboard locally with fake people and history, without Slack.

    python -m chargebot.devserver [--chargers 2] [--port 8080]

Prints a magic link (acting as "You") and a kiosk link. Nothing is sent anywhere.
"""

from __future__ import annotations

import argparse
import random
import time

from .config import Config
from .engine import Engine
from .scheduler import Scheduler
from .slack.users import Profile
from .store import MemoryStore
from .web.server import WebApp, start_server
from .web.tokens import make_token

PEOPLE = {
    "UME": "Yossi Oren", "U1": "Noa Levi", "U2": "Dana Shapiro", "U3": "Avi Cohen", "U4": "Maya Katz",
    "U5": "Omer Ben-David", "U6": "Tamar Golan", "U7": "<img src=x onerror=alert(1)>",
}


def fake_history(now: float, days: int = 90):
    rng = random.Random(7)
    records = []
    n = 0
    for d in range(days, 0, -1):
        day = now - d * 86400
        if time.localtime(day).tm_wday in (4, 5):  # quieter Fri/Sat
            continue
        t = day - (day % 86400) + 6 * 3600 + rng.randint(0, 3600)
        for _ in range(rng.randint(2, 5)):
            n += 1
            joined = t - rng.randint(0, 40) * 60
            outcome = rng.choices(["completed", "ended_early", "no_show"], [70, 22, 8])[0]
            confirmed = None if outcome == "no_show" else t + rng.randint(1, 8) * 60
            length = 120 * 60 if outcome == "completed" else rng.randint(30, 110) * 60
            ended = t + 600 if outcome == "no_show" else confirmed + length
            records.append({"id": f"h{n}", "user_id": rng.choice(list(PEOPLE)[:7]), "charger_id": "c1",
                            "joined_at": joined, "offered_at": t, "confirmed_at": confirmed,
                            "charge_start": confirmed, "ended_at": ended, "outcome": outcome, "extensions": 0})
            t = ended + rng.randint(5, 60) * 60
    return records


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--chargers", type=int, default=1)
    args = parser.parse_args()

    names = tuple(f"Charger {i + 1}" for i in range(args.chargers))
    cfg = Config(web_secret="dev-secret", kiosk_key="dev-kiosk", chargers=names, port=args.port,
                 slack_app_id="A0DEV", slack_team_id="T0DEV", tick_s=2)
    store = MemoryStore()
    store.log = fake_history(time.time())
    engine = Engine(cfg, store)
    engine.load()

    # Someone charging for a while, someone queued, and you in line.
    engine.act("join", "U1")
    engine.act("confirm", "U1")
    with engine._lock:
        s = engine.state.chargers[0].session
        s.charge_start -= 82 * 60
        s.ends_at -= 82 * 60
    if args.chargers > 1:
        engine.act("join", "U3")
    engine.act("join", "U7")
    engine.act("join", "UME")
    engine.act("join", "U2")

    def profile(uid: str) -> Profile:
        return Profile(uid, PEOPLE.get(uid, uid))

    start_server(WebApp(cfg, engine, profile), cfg.port)
    Scheduler(engine, cfg.tick_s).start()
    base = f"http://localhost:{cfg.port}"
    print(f"Dashboard:        {base}/")
    print(f"As 'You' (Yossi): {base}/?t={make_token(cfg.web_secret, 'UME')}")
    print(f"As Noa (charging):{base}/?t={make_token(cfg.web_secret, 'U1')}")
    print(f"Kiosk:            {base}/kiosk?k={cfg.kiosk_key}")
    print(f"Stats:            {base}/stats")
    print("Try: end Noa's session from her link to see the 'your turn' flow move down the queue.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
