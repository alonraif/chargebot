"""Zero-infrastructure persistence inside a private Slack channel.

* The state snapshot lives in the metadata of a single bot message, which is
  edited in place (``chat.update``) whenever the state changes.
* Each finished session is posted as its own message with metadata. The
  messages double as a human-readable audit log.

On startup, the channel history is read back with ``include_all_metadata``.
Writes happen on a background thread, so callers never wait on Slack.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Any, Dict, List, Optional

from slack_sdk.errors import SlackApiError

log = logging.getLogger(__name__)

STATE_EVENT = "chargebot_state"
SESSION_EVENT = "chargebot_session"
MAX_HISTORY_PAGES = 40
# Edited messages keep their original timestamp, so the snapshot is reposted
# periodically to keep it near the top of the channel history.
SNAPSHOT_REPOST_AGE_S = 30 * 86400


def _metadata(event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    # The payload is stored as a JSON string so Slack never reshapes nested values.
    return {"event_type": event_type, "event_payload": {"v": 1, "json": json.dumps(payload, separators=(",", ":"))}}


def _payload(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(message["metadata"]["event_payload"]["json"])
    except (KeyError, TypeError, ValueError):
        return None


class SlackStore:
    def __init__(self, client: Any, channel_id: str, debounce_s: float = 1.0) -> None:
        self.client = client
        self.channel_id = channel_id
        self.debounce_s = debounce_s
        self._snapshot_ts: Optional[str] = None
        self._log: List[Dict[str, Any]] = []
        self._pending: Optional[Dict[str, Any]] = None
        self._jobs: "queue.Queue[Any]" = queue.Queue()
        self._cond = threading.Condition()
        self._busy = False
        self._worker = threading.Thread(target=self._run, name="slack-store", daemon=True)
        self._worker.start()

    # -- reading ------------------------------------------------------------

    def load(self) -> Optional[Dict[str, Any]]:
        snapshot = None
        records: List[Dict[str, Any]] = []
        cursor = None
        for _ in range(MAX_HISTORY_PAGES):
            resp = self.client.conversations_history(
                channel=self.channel_id, include_all_metadata=True, limit=200, cursor=cursor)
            for msg in resp.get("messages", []):
                event_type = (msg.get("metadata") or {}).get("event_type")
                if event_type == STATE_EVENT and snapshot is None:
                    snapshot = _payload(msg)
                    if snapshot is not None:
                        self._snapshot_ts = msg["ts"]
                elif event_type == SESSION_EVENT:
                    record = _payload(msg)
                    if record is not None:
                        records.append(record)
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        records.reverse()  # history is newest-first
        self._log = records
        log.info("Loaded state from Slack (snapshot=%s, %d session records).",
                 "found" if snapshot else "none", len(records))
        return snapshot

    def load_log(self) -> List[Dict[str, Any]]:
        return list(self._log)

    # -- writing ------------------------------------------------------------

    def save(self, snapshot: Dict[str, Any]) -> None:
        with self._cond:
            self._pending = snapshot
            self._cond.notify_all()
        self._jobs.put("snapshot")

    def append_log(self, record: Dict[str, Any], summary: str) -> None:
        self._log.append(dict(record))
        self._jobs.put(("log", record, summary))

    def flush(self, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._cond:
                if self._jobs.empty() and not self._busy and self._pending is None:
                    return
            time.sleep(0.05)

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            with self._cond:
                self._busy = True
            try:
                if job == "snapshot":
                    time.sleep(self.debounce_s)
                    with self._cond:
                        snapshot, self._pending = self._pending, None
                    if snapshot is not None:
                        self._write_snapshot(snapshot)
                else:
                    _, record, summary = job
                    self._write_log(record, summary)
            except Exception:
                log.exception("Slack store write failed.")
            finally:
                with self._cond:
                    self._busy = False

    def _write_snapshot(self, snapshot: Dict[str, Any]) -> None:
        text = (f":card_file_box: ChargeBot state v{snapshot.get('version', 0)}. "
                "Please don't edit or delete this message; the bot keeps its memory here.")
        metadata = _metadata(STATE_EVENT, snapshot)
        if self._snapshot_ts and time.time() - float(self._snapshot_ts) > SNAPSHOT_REPOST_AGE_S:
            old_ts, self._snapshot_ts = self._snapshot_ts, None
            resp = self.client.chat_postMessage(channel=self.channel_id, text=text, metadata=metadata)
            self._snapshot_ts = resp["ts"]
            try:
                self.client.chat_delete(channel=self.channel_id, ts=old_ts)
            except SlackApiError as e:
                log.warning("Could not delete old state message (%s).", e.response.get("error"))
            return
        if self._snapshot_ts:
            try:
                self.client.chat_update(channel=self.channel_id, ts=self._snapshot_ts, text=text, metadata=metadata)
                return
            except SlackApiError as e:
                log.warning("Could not update state message (%s); posting a new one.", e.response.get("error"))
        resp = self.client.chat_postMessage(channel=self.channel_id, text=text, metadata=metadata)
        self._snapshot_ts = resp["ts"]

    def _write_log(self, record: Dict[str, Any], summary: str) -> None:
        self.client.chat_postMessage(
            channel=self.channel_id, text=summary, metadata=_metadata(SESSION_EVENT, record))
