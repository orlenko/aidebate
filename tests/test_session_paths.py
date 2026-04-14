"""Session ID format + sessions_root resolution."""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from aidebate.core.session import new_session_id, sessions_root


def test_new_session_id_matches_timestamp_format():
    sid = new_session_id()
    assert re.match(r"^\d{4}-\d{2}-\d{2}-\d{6}$", sid)


def test_sessions_root_honours_aidebate_home(sessions_dir: Path):
    # The sessions_dir fixture sets AIDEBATE_HOME to a tmp dir.
    expected = Path(os.environ["AIDEBATE_HOME"]) / "sessions"
    assert sessions_root() == expected


def test_sessions_root_default_without_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("AIDEBATE_HOME", raising=False)
    root = sessions_root()
    # Landing somewhere platform-appropriate that mentions aidebate is enough.
    assert "aidebate" in str(root)
    assert root.name == "sessions"
