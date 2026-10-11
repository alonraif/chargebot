"""Typed configuration loaded from environment variables."""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field
from typing import Mapping, Optional, Tuple

log = logging.getLogger(__name__)


def _minutes(env: Mapping[str, str], key: str, default: float) -> int:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return int(default * 60)
    return int(float(raw) * 60)


def _list(env: Mapping[str, str], key: str, default: str = "") -> Tuple[str, ...]:
    raw = env.get(key, default) or ""
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class Config:
    slack_bot_token: str = ""
    slack_app_token: str = ""

    # Chargers. One entry per physical charger; add a name to add a charger.
    chargers: Tuple[str, ...] = ("Charger 1",)

    # Session timing, in seconds.
    charge_s: int = 120 * 60
    grace_s: int = 5 * 60
    confirm_s: int = 10 * 60
    confirm_alerts_s: Tuple[int, ...] = (5 * 60, 8 * 60)
    warn_before_end_s: int = 10 * 60
    extend_s: int = 30 * 60
    max_extensions: int = 2
    next_up_heads_up_s: int = 15 * 60
    nudge_cooldown_s: int = 5 * 60

    # Slack channels and people.
    state_channel_id: Optional[str] = None
    status_channel_id: Optional[str] = None
    admin_user_ids: frozenset = field(default_factory=frozenset)

    # Web.
    port: int = 8080
    public_base_url: Optional[str] = None
    web_secret: str = ""
    kiosk_key: Optional[str] = None
    device_link_days: int = 90
    slack_app_id: Optional[str] = None
    slack_team_id: Optional[str] = None

    # Local development persistence (used when no state channel is set).
    state_file: Optional[str] = None

    # Gmail calendar reminders (optional).
    gmail_client_id: Optional[str] = None
    gmail_client_secret: Optional[str] = None
    gmail_refresh_token: Optional[str] = None
    gmail_token_uri: str = "https://oauth2.googleapis.com/token"
    gmail_from_email: Optional[str] = None

    tick_s: float = 5.0
    timezone: str = "Asia/Jerusalem"

    @property
    def multi_charger(self) -> bool:
        return len(self.chargers) > 1

    @property
    def gmail_ready(self) -> bool:
        return all((
            self.gmail_client_id,
            self.gmail_client_secret,
            self.gmail_refresh_token,
            self.gmail_token_uri,
            self.gmail_from_email,
        ))

    def is_admin(self, user_id: str) -> bool:
        return user_id in self.admin_user_ids

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Config":
        env = os.environ if env is None else env

        web_secret = env.get("WEB_SECRET", "")
        if not web_secret:
            web_secret = secrets.token_urlsafe(32)
            log.warning("WEB_SECRET is not set; dashboard links will stop working after a restart.")

        alerts = tuple(int(float(m) * 60) for m in _list(env, "CONFIRM_ALERT_AT_MIN", "5,8"))
        base_url = (env.get("PUBLIC_BASE_URL") or "").rstrip("/") or None

        return cls(
            slack_bot_token=env.get("SLACK_BOT_TOKEN", ""),
            slack_app_token=env.get("SLACK_APP_TOKEN", ""),
            chargers=_list(env, "CHARGERS", "Charger 1") or ("Charger 1",),
            charge_s=_minutes(env, "CHARGE_MIN", 120),
            grace_s=_minutes(env, "GRACE_MIN", 5),
            confirm_s=_minutes(env, "CONFIRM_MIN", 10),
            confirm_alerts_s=alerts,
            warn_before_end_s=_minutes(env, "WARN_BEFORE_END_MIN", 10),
            extend_s=_minutes(env, "EXTEND_MIN", 30),
            max_extensions=int(env.get("MAX_EXTENSIONS", "2")),
            next_up_heads_up_s=_minutes(env, "NEXT_UP_HEADS_UP_MIN", 15),
            nudge_cooldown_s=_minutes(env, "NUDGE_COOLDOWN_MIN", 5),
            state_channel_id=env.get("STATE_CHANNEL_ID") or None,
            status_channel_id=env.get("STATUS_CHANNEL_ID") or None,
            admin_user_ids=frozenset(_list(env, "ADMIN_USER_IDS")),
            port=int(env.get("PORT", "8080")),
            public_base_url=base_url,
            web_secret=web_secret,
            kiosk_key=env.get("KIOSK_KEY") or None,
            device_link_days=int(env.get("DEVICE_LINK_DAYS", "90")),
            slack_app_id=env.get("SLACK_APP_ID") or None,
            slack_team_id=env.get("SLACK_TEAM_ID") or None,
            state_file=env.get("STATE_FILE") or None,
            gmail_client_id=env.get("GMAIL_CLIENT_ID") or None,
            gmail_client_secret=env.get("GMAIL_CLIENT_SECRET") or None,
            gmail_refresh_token=env.get("GMAIL_REFRESH_TOKEN") or None,
            gmail_token_uri=env.get("GMAIL_TOKEN_URI") or "https://oauth2.googleapis.com/token",
            gmail_from_email=env.get("GMAIL_FROM_EMAIL") or None,
            tick_s=float(env.get("TICK_SECONDS", "5")),
            timezone=env.get("TIMEZONE") or "Asia/Jerusalem",
        )
