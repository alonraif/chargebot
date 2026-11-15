"""Export the full charging-session history from the Slack state channel to CSV.

Usage (needs the bot token; the bot must be in the channel):

    SLACK_BOT_TOKEN=xoxb-... STATE_CHANNEL_ID=C0C8H4UAK1S \
        python -m chargebot.export_csv sessions.csv [--tz Asia/Jerusalem]

Reads every page of channel history (no page cap), decodes each session record
the same way the bot does, de-duplicates reclaimed turns, and writes one row per
session. Times are written as ISO timestamps in the chosen timezone.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from chargebot.store.slack_store import SESSION_EVENT, _decode_text, _payload
from chargebot.stats import _dedupe, _tz

TIME_FIELDS = ("joined_at", "offered_at", "confirmed_at", "charge_start", "ended_at")


def fetch_records(client: WebClient, channel: str) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    while True:
        try:
            resp = client.conversations_history(
                channel=channel, include_all_metadata=True, limit=200, cursor=cursor)
        except SlackApiError as e:
            if e.response.get("error") == "ratelimited":
                time.sleep(int(e.response.headers.get("Retry-After", "5")))
                continue
            raise
        for msg in resp.get("messages", []):
            data = None
            if (msg.get("metadata") or {}).get("event_type") == SESSION_EVENT:
                data = _payload(msg)
            if data is None:
                decoded = _decode_text(msg)
                if decoded and decoded[0] == SESSION_EVENT:
                    data = decoded[1]
            if data is not None:
                data["_slack_ts"] = msg["ts"]
                records.append(data)
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    records.reverse()  # newest-first -> oldest-first
    return records


def user_names(client: WebClient, ids: List[str]) -> Dict[str, str]:
    names: Dict[str, str] = {}
    for uid in ids:
        try:
            p = client.users_info(user=uid)["user"]
            names[uid] = p.get("real_name") or p.get("name") or uid
        except SlackApiError:
            names[uid] = uid  # missing users:read or deactivated user
    return names


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("out", help="CSV file to write")
    ap.add_argument("--tz", default=os.environ.get("TIMEZONE", "Asia/Jerusalem"))
    ap.add_argument("--channel", default=os.environ.get("STATE_CHANNEL_ID"))
    args = ap.parse_args()

    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token or not args.channel:
        print("Set SLACK_BOT_TOKEN and STATE_CHANNEL_ID (or pass --channel).", file=sys.stderr)
        return 2

    client = WebClient(token=token)
    records = _dedupe(fetch_records(client, args.channel))
    records.sort(key=lambda r: r.get("joined_at") or 0)
    tz = _tz(args.tz)
    names = user_names(client, sorted({r["user_id"] for r in records if r.get("user_id")}))

    def iso(t: Any) -> str:
        return datetime.fromtimestamp(t, tz).isoformat(timespec="seconds") if t else ""

    cols = ["session_id", "user_id", "user_name", "charger_id", *TIME_FIELDS, "extensions", "outcome",
            "wait_min", "charge_min", "total_min", "capped", "strikes", "week_charges_before"]
    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:  # BOM so Excel reads UTF-8
        w = csv.writer(f)
        w.writerow(cols)
        for r in records:
            j, o, s, e = r.get("joined_at"), r.get("offered_at"), r.get("charge_start"), r.get("ended_at")
            w.writerow([
                r.get("id", ""), r.get("user_id", ""), names.get(r.get("user_id"), ""), r.get("charger_id", ""),
                *(iso(r.get(k)) for k in TIME_FIELDS), r.get("extensions", ""), r.get("outcome", ""),
                round((o - j) / 60, 1) if j and o and o >= j else "",
                round((e - s) / 60, 1) if s and e and e >= s else "",
                round((e - j) / 60, 1) if j and e and e >= j else "",
                r.get("capped", ""), r.get("strikes", ""), r.get("week_charges_before", ""),  # blank on old records
            ])
    print(f"Wrote {len(records)} sessions to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
