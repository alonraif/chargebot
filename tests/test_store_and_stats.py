from __future__ import annotations

import os
import tempfile

from slack_sdk.errors import SlackApiError

from chargebot.config import Config
from chargebot.reminders.gmail import build_email, build_ics
from chargebot.stats import global_stats, user_stats
from chargebot.store import FileStore, SlackStore
from chargebot.store.slack_store import SESSION_EVENT, STATE_EVENT


class FakeSlack:
    def __init__(self):
        self.messages = []  # newest last
        self.n = 0

    def chat_postMessage(self, channel, text, metadata=None, **kw):
        self.n += 1
        ts = f"{1_790_000_000 + self.n}.000100"
        self.messages.append({"ts": ts, "text": text, "metadata": metadata, "bot_id": "B1"})
        return {"ts": ts, "channel": channel}

    def chat_update(self, channel, ts, text, metadata=None, **kw):
        for m in self.messages:
            if m["ts"] == ts:
                m.update(text=text, metadata=metadata)
                return {"ok": True}
        raise SlackApiError("not found", {"error": "message_not_found"})

    def chat_delete(self, channel, ts):
        self.messages = [m for m in self.messages if m["ts"] != ts]

    def conversations_history(self, channel, include_all_metadata, limit, cursor=None):
        newest_first = list(reversed(self.messages))
        start = int(cursor or 0)
        page = newest_first[start:start + 3]  # small pages to exercise paging
        nxt = str(start + 3) if start + 3 < len(newest_first) else ""
        return {"messages": page, "response_metadata": {"next_cursor": nxt}}


def test_slack_store_round_trip_and_paging():
    slack = FakeSlack()
    store = SlackStore(slack, "C1", debounce_s=0)
    store.save({"version": 1, "queue": []})
    store.save({"version": 2, "queue": [{"user_id": "U1"}]})
    for i in range(7):
        store.append_log({"id": f"s{i}", "outcome": "completed"}, f"session {i}")
    store.flush()
    state_msgs = [m for m in slack.messages if m["metadata"]["event_type"] == STATE_EVENT]
    assert len(state_msgs) == 1  # updated in place
    assert sum(m["metadata"]["event_type"] == SESSION_EVENT for m in slack.messages) == 7

    again = SlackStore(slack, "C1", debounce_s=0)
    snap = again.load()
    assert snap["version"] == 2 and snap["queue"][0]["user_id"] == "U1"
    assert [r["id"] for r in again.load_log()] == [f"s{i}" for i in range(7)]


def test_slack_store_reposts_when_state_message_deleted():
    slack = FakeSlack()
    store = SlackStore(slack, "C1", debounce_s=0)
    store.save({"version": 1})
    store.flush()
    slack.messages.clear()
    store.save({"version": 2})
    store.flush()
    assert len(slack.messages) == 1


def test_file_store_round_trip():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "state.json")
        store = FileStore(path)
        assert store.load() is None
        store.save({"version": 3})
        store.append_log({"id": "s1"}, "x")
        assert FileStore(path).load() == {"version": 3}
        assert FileStore(path).load_log() == [{"id": "s1"}]


def test_stats():
    now = 1_790_000_000.0
    log = [
        {"id": "s1", "user_id": "U1", "joined_at": now - 9000, "offered_at": now - 8400, "charge_start": now - 8300,
         "ended_at": now - 1100, "outcome": "completed"},
        {"id": "s2", "user_id": "U2", "joined_at": now - 1000, "offered_at": now - 1000, "charge_start": None,
         "ended_at": now - 400, "outcome": "no_show"},
        {"id": "s2", "user_id": "U2", "joined_at": now - 1000, "offered_at": now - 1000, "charge_start": now - 300,
         "ended_at": now, "outcome": "ended_early"},  # reclaimed: same id, latest wins
    ]
    g = global_stats(log, now, "Asia/Jerusalem")
    assert g["sessions"] == 2
    assert g["no_show_rate"] == 0.0
    assert g["avg_wait_min"] == 5
    assert sum(sum(r) for r in g["heatmap"]) == 125
    assert user_stats(log, "U1") == {"sessions": 1, "no_shows": 0, "avg_wait_min": 10}


def test_ics_request_and_cancel():
    req = build_ics("uid-1", "bot@x.com", "a@x.com", 1.79e9, 1.79e9 + 300, sequence=1)
    assert "METHOD:REQUEST" in req and "SEQUENCE:1" in req and "BEGIN:VALARM" in req
    cancel = build_ics("uid-1", "bot@x.com", "a@x.com", 1.79e9, 1.79e9 + 300, sequence=2, cancel=True)
    assert "METHOD:CANCEL" in cancel and "STATUS:CANCELLED" in cancel and "VALARM" not in cancel
    msg = build_email(Config(gmail_from_email="bot@x.com"), "a@x.com", "uid-1", 1.79e9, 1.79e9 + 300, cancel=True)
    assert msg["Subject"].startswith("Cancelled")


def test_slack_store_reads_back_from_text_when_metadata_is_missing():
    slack = FakeSlack()
    store = SlackStore(slack, "C1", debounce_s=0)
    store.save({"version": 5, "prefs": {"U1": {"push": [{"endpoint": "https://web.push.apple.com/x"}]}}})
    store.append_log({"id": "s1", "outcome": "completed"}, "Noa: charged")
    store.flush()
    for m in slack.messages:  # simulate Slack ignoring the metadata
        m["metadata"] = None
        assert "https://" not in m["text"]  # nothing Slack could turn into a link
    again = SlackStore(slack, "C1", debounce_s=0)
    snap = again.load()
    assert snap["version"] == 5 and snap["prefs"]["U1"]["push"][0]["endpoint"].startswith("https://")
    assert again.load_log() == [{"id": "s1", "outcome": "completed"}]
