from __future__ import annotations

import pytest

from chargebot.config import Config
from chargebot.engine import Engine
from chargebot.store import MemoryStore

MIN = 60


class Clock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, minutes: float) -> None:
        self.now += minutes * MIN


class Harness:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.clock = Clock()
        self.store = MemoryStore()
        self.engine = Engine(cfg, self.store, clock=self.clock)
        self.effects: list = []
        self.engine.set_dispatcher(self.effects.extend)
        self.engine.load()

    def act(self, action: str, user: str):
        return self.engine.act(action, user)

    def wait(self, minutes: float):
        """Advance time in 1-minute steps, ticking like the scheduler would."""
        steps = int(minutes)
        for _ in range(steps):
            self.clock.advance(1)
            self.engine.tick()
        rest = minutes - steps
        if rest:
            self.clock.advance(rest)
            self.engine.tick()

    def notes(self, user: str | None = None, kind: str | None = None):
        from chargebot.domain import Notify
        return [e for e in self.effects if isinstance(e, Notify)
                and (user is None or e.user_id == user) and (kind is None or e.kind == kind)]

    def of(self, cls):
        return [e for e in self.effects if isinstance(e, cls)]

    def clear(self):
        self.effects.clear()

    @property
    def state(self):
        return self.engine.state


@pytest.fixture
def h() -> Harness:
    return Harness(Config(web_secret="test"))


@pytest.fixture
def h2() -> Harness:
    return Harness(Config(web_secret="test", chargers=("Charger 1", "Charger 2")))
