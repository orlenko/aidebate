"""Structured narrative event log for a debate session."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class EventLog:
    """Append-only JSONL log of narrative events (phase transitions,
    participant milestones, chat activity) for one debate session.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def emit(self, type: str, **fields: object) -> dict:
        event: dict = {"ts": _ts(), "type": type, **fields}
        line = json.dumps(event, ensure_ascii=False)
        with self._lock, self.path.open("a") as f:
            f.write(line + "\n")
        return event


def read_events(path: Path) -> list[dict]:
    """Parse events.jsonl, skipping malformed lines."""
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
