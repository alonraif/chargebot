from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional


class MemoryStore:
    """Keeps everything in process memory. For tests, and as a last-resort fallback."""

    def __init__(self) -> None:
        self.snapshot: Optional[Dict[str, Any]] = None
        self.log: List[Dict[str, Any]] = []
        self.saves = 0

    def load(self) -> Optional[Dict[str, Any]]:
        return copy.deepcopy(self.snapshot)

    def save(self, snapshot: Dict[str, Any]) -> None:
        self.snapshot = copy.deepcopy(snapshot)
        self.saves += 1

    def append_log(self, record: Dict[str, Any], summary: str) -> None:
        self.log.append(dict(record))

    def load_log(self) -> List[Dict[str, Any]]:
        return list(self.log)

    def flush(self, timeout: float = 5.0) -> None:
        pass
