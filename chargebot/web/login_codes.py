"""Short one-time sign-in codes (e.g. K7Q-4ZP) for browsers that can't open a Slack link directly,
such as the dashboard installed on an iPhone home screen. Kept in memory: a code lives 10 minutes."""

from __future__ import annotations

import secrets
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L
CODE_LEN = 6
CODE_TTL_S = 10 * 60
FAIL_WINDOW_S = 10 * 60
MAX_FAILS_PER_CLIENT = 10
MAX_FAILS_TOTAL = 100


def normalize(code: Optional[str]) -> str:
    return "".join(ch for ch in (code or "").upper() if ch in ALPHABET)


def pretty(code: str) -> str:
    return f"{code[:3]}-{code[3:]}"


class LoginCodes:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._codes: Dict[str, Tuple[str, float]] = {}
        self._fails: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def issue(self, user_id: str) -> str:
        """A fresh code for this user (their older unused codes stop working)."""
        now = self.clock()
        with self._lock:
            self._prune(now)
            for code in [c for c, (u, _) in self._codes.items() if u == user_id]:
                del self._codes[code]
            code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))
            self._codes[code] = (user_id, now + CODE_TTL_S)
        return pretty(code)

    def redeem(self, code: Optional[str], client: str = "?") -> Tuple[Optional[str], str]:
        """Return (user_id, "ok") for a valid code, which is then used up; else (None, reason)."""
        now = self.clock()
        key = normalize(code)
        with self._lock:
            self._prune(now)
            recent = self._fails.get(client, [])
            total = sum(len(v) for v in self._fails.values())
            if len(recent) >= MAX_FAILS_PER_CLIENT or total >= MAX_FAILS_TOTAL:
                return None, "too_many_attempts"
            found = self._codes.pop(key, None) if len(key) == CODE_LEN else None
            if not found:
                self._fails.setdefault(client, []).append(now)
                return None, "bad_code"
            return found[0], "ok"

    def _prune(self, now: float) -> None:
        for code in [c for c, (_, exp) in self._codes.items() if exp < now]:
            del self._codes[code]
        for client in list(self._fails):
            self._fails[client] = [t for t in self._fails[client] if t > now - FAIL_WINDOW_S]
            if not self._fails[client]:
                del self._fails[client]
