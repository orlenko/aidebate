"""Wait for flag files to appear."""
from __future__ import annotations

import time
from pathlib import Path


def wait_for_file(path: Path, timeout: float = 600.0, poll: float = 0.5) -> bool:
    """Poll for a file's existence. Returns True on success, False on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(poll)
    return False


def wait_for_all(paths: list[Path], timeout: float = 600.0, poll: float = 0.5) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if all(p.exists() for p in paths):
            return True
        time.sleep(poll)
    return False
