"""A single clock loop that drives every deadline (grace, confirm, warnings, session end)."""

from __future__ import annotations

import logging
import threading

from .engine import Engine

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, engine: Engine, interval_s: float) -> None:
        self.engine = engine
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="scheduler", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        log.info("Scheduler started (every %.0fs).", self.interval_s)
        while not self._stop.is_set():
            try:
                self.engine.tick()
            except Exception:
                log.exception("Scheduler tick failed.")
            self._stop.wait(self.interval_s)
