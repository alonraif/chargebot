"""Calendar-invite reminders ("disconnect your car") sent through the Gmail API."""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Optional

from ..config import Config

log = logging.getLogger(__name__)

SUMMARY = "Disconnect car from charger"
DESCRIPTION = "Your charging session has ended. Please disconnect your car from the shared charger."


def _ics_time(unix_ts: float) -> str:
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _ics_text(value: Optional[str]) -> str:
    if value is None:
        return ""
    return (str(value).replace("\\", "\\\\").replace(";", r"\;").replace(",", r"\,")
            .replace("\r\n", r"\n").replace("\n", r"\n"))


def build_ics(uid: str, organizer: str, attendee: str, start: float, end: float,
              sequence: int = 0, cancel: bool = False) -> str:
    method = "CANCEL" if cancel else "REQUEST"
    lines = [
        "BEGIN:VCALENDAR",
        "PRODID:-//ChargeBot//Disconnect Reminder//EN",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        f"METHOD:{method}",
        "BEGIN:VEVENT",
        f"UID:{_ics_text(uid)}",
        f"SEQUENCE:{sequence}",
        f"DTSTAMP:{_ics_time(time.time())}",
        f"DTSTART:{_ics_time(start)}",
        f"DTEND:{_ics_time(end)}",
        f"SUMMARY:{_ics_text(SUMMARY)}",
        f"DESCRIPTION:{_ics_text(DESCRIPTION)}",
        f"ORGANIZER:mailto:{_ics_text(organizer)}",
        f"ATTENDEE;ROLE=REQ-PARTICIPANT;RSVP=TRUE:mailto:{_ics_text(attendee)}",
        "STATUS:CANCELLED" if cancel else "STATUS:CONFIRMED",
        "TRANSP:TRANSPARENT",
    ]
    if not cancel:
        lines += ["BEGIN:VALARM", "TRIGGER:-PT5M", "ACTION:DISPLAY", f"DESCRIPTION:{_ics_text(SUMMARY)}",
                  "END:VALARM"]
    lines += ["END:VEVENT", "END:VCALENDAR", ""]
    return "\r\n".join(lines)


def build_email(cfg: Config, recipient: str, uid: str, start: float, end: float,
                sequence: int = 0, cancel: bool = False) -> EmailMessage:
    ics = build_ics(uid, cfg.gmail_from_email or "", recipient, start, end, sequence, cancel)
    msg = EmailMessage()
    msg["From"] = cfg.gmail_from_email
    msg["To"] = recipient
    if cancel:
        msg["Subject"] = "Cancelled: disconnect reminder"
        msg.set_content("Your charging session ended early, so this reminder is no longer needed.")
    else:
        msg["Subject"] = "Reminder: disconnect your car from the charger"
        msg.set_content("A calendar invite is attached as a reminder to disconnect your car when your "
                        "charging session ends.")
        msg.add_alternative("<p>A calendar invite is attached as a reminder to disconnect your car when your "
                            "charging session ends.</p>", subtype="html")
    method = "CANCEL" if cancel else "REQUEST"
    msg.add_attachment(ics.encode("utf-8"), maintype="text", subtype="calendar",
                       filename="disconnect-reminder.ics", params={"method": method, "charset": "UTF-8"})
    return msg


class GmailSender:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    @property
    def ready(self) -> bool:
        return self.cfg.gmail_ready

    def _access_token(self) -> str:
        data = urllib.parse.urlencode({
            "client_id": self.cfg.gmail_client_id,
            "client_secret": self.cfg.gmail_client_secret,
            "refresh_token": self.cfg.gmail_refresh_token,
            "grant_type": "refresh_token",
        }).encode()
        req = urllib.request.Request(self.cfg.gmail_token_uri, data=data, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            token = json.loads(resp.read().decode()).get("access_token")
        if not token:
            raise RuntimeError("Gmail token response had no access token.")
        return token

    def send(self, recipient: str, uid: str, start: float, end: float,
             sequence: int = 0, cancel: bool = False) -> None:
        msg = build_email(self.cfg, recipient, uid, start, end, sequence, cancel)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        req = urllib.request.Request(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            data=json.dumps({"raw": raw}).encode(), method="POST",
            headers={"Authorization": f"Bearer {self._access_token()}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20):
            pass
        log.info("Sent %s reminder %s (sequence %d).", "cancel" if cancel else "invite", uid, sequence)
