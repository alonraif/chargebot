"""Thread-safe cache of Slack profiles (name, avatar, email)."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

TTL_S = 60 * 60
ERROR_TTL_S = 60


@dataclass(frozen=True)
class Profile:
    user_id: str
    name: str
    avatar: Optional[str] = None
    email: Optional[str] = None


class UserDirectory:
    def __init__(self, client: Any) -> None:
        self.client = client
        self._cache: Dict[str, tuple] = {}
        self._lock = threading.Lock()

    def get(self, user_id: str) -> Profile:
        now = time.time()
        with self._lock:
            hit = self._cache.get(user_id)
            if hit and hit[1] > now:
                return hit[0]
        profile, ttl = self._fetch(user_id)
        with self._lock:
            self._cache[user_id] = (profile, now + ttl)
        return profile

    def name(self, user_id: Optional[str]) -> str:
        return self.get(user_id).name if user_id else "Someone"

    def _fetch(self, user_id: str) -> tuple:
        try:
            resp = self.client.users_info(user=user_id)
            user = resp["user"]
            p = user.get("profile", {})
            name = p.get("display_name") or user.get("real_name") or p.get("real_name") or user_id
            avatar = p.get("image_72") or p.get("image_48")
            return Profile(user_id, name, avatar, p.get("email")), TTL_S
        except Exception as e:
            log.warning("Could not look up Slack user %s: %s", user_id, e)
            return Profile(user_id, user_id), ERROR_TTL_S
