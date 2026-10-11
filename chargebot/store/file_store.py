from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


class FileStore:
    """JSON snapshot plus a JSON-lines session log on local disk. Handy for local development."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.log_path = path + ".log.jsonl"
        self._lock = threading.Lock()

    def load(self) -> Optional[Dict[str, Any]]:
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            log.exception("Could not read state file %s; starting empty.", self.path)
            return None

    def save(self, snapshot: Dict[str, Any]) -> None:
        tmp = self.path + ".tmp"
        with self._lock:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, separators=(",", ":"))
            os.replace(tmp, self.path)

    def append_log(self, record: Dict[str, Any], summary: str) -> None:
        with self._lock, open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")

    def load_log(self) -> List[Dict[str, Any]]:
        records = []
        try:
            with open(self.log_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            records.append(json.loads(line))
                        except ValueError:
                            log.warning("Skipping bad session log line.")
        except FileNotFoundError:
            pass
        return records

    def flush(self, timeout: float = 5.0) -> None:
        pass
