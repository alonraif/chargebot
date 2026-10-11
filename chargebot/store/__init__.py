"""Persistence backends.

A store keeps two things: the latest state snapshot (overwritten on every
change) and an append-only log of finished sessions (used for stats).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

try:  # Python 3.8+
    from typing import Protocol
except ImportError:  # pragma: no cover
    Protocol = object  # type: ignore


class Store(Protocol):
    def load(self) -> Optional[Dict[str, Any]]: ...

    def save(self, snapshot: Dict[str, Any]) -> None: ...

    def append_log(self, record: Dict[str, Any], summary: str) -> None: ...

    def load_log(self) -> List[Dict[str, Any]]: ...

    def flush(self, timeout: float = 5.0) -> None: ...


from .file_store import FileStore  # noqa: E402
from .memory_store import MemoryStore  # noqa: E402
from .slack_store import SlackStore  # noqa: E402

__all__ = ["Store", "MemoryStore", "FileStore", "SlackStore"]
