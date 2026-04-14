"""Shared test fixtures."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def sessions_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point AIDEBATE_HOME at a temp dir so tests never touch real user data."""
    monkeypatch.setenv("AIDEBATE_HOME", str(tmp_path))
    return tmp_path / "sessions"
