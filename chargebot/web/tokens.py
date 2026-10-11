"""Signed, expiring dashboard passes that act on behalf of one Slack user.

A pass carries the user id, an expiry and a "generation" number. Bumping a
user's generation (Slack Home → sign out everywhere) invalidates every pass
they were given before.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Optional, Tuple

DEFAULT_TTL_S = 24 * 3600


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(secret: str, payload: str) -> str:
    return _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()[:18])


def make_token(secret: str, user_id: str, ttl_s: int = DEFAULT_TTL_S, now: Optional[float] = None,
               gen: int = 0) -> str:
    exp = int((now if now is not None else time.time()) + ttl_s)
    payload = _b64(f"{user_id}|{exp}|{gen}".encode())
    return f"{payload}.{_sign(secret, payload)}"


def read_token(secret: str, token: Optional[str], now: Optional[float] = None) -> Optional[Tuple[str, int]]:
    """Return (user_id, generation) if the pass is genuine and unexpired, else None."""
    if not token or token.count(".") != 1:
        return None
    payload, sig = token.split(".")
    if not hmac.compare_digest(sig, _sign(secret, payload)):
        return None
    try:
        parts = _unb64(payload).decode().split("|")
        if len(parts) == 2:  # passes issued before generations existed
            parts.append("0")
        user_id, exp, gen = parts
        if int(exp) < (now if now is not None else time.time()):
            return None
        return (user_id, int(gen)) if user_id else None
    except (ValueError, UnicodeDecodeError):
        return None


def verify_token(secret: str, token: Optional[str], now: Optional[float] = None) -> Optional[str]:
    """Return the user id if the pass is genuine and unexpired (ignores generation)."""
    found = read_token(secret, token, now)
    return found[0] if found else None


def check_kiosk_key(expected: Optional[str], given: Optional[str]) -> bool:
    return bool(expected) and bool(given) and hmac.compare_digest(expected, given)
