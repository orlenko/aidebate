"""Session ID format + sessions_root resolution."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from aidebate.core.session import new_session_id, sessions_root


def test_new_session_id_matches_timestamp_format():
    sid = new_session_id()
    assert re.match(r"^\d{4}-\d{2}-\d{2}-\d{6}-\d{3}$", sid)


def test_new_session_id_is_unique_within_a_second():
    """With ms precision, IDs generated a few ms apart must differ, even
    though they share a whole-second prefix. This is the realistic debate-
    spawn cadence (two HTTP requests, ≥1ms apart) — not a tight loop."""
    import time as _time

    ids: list[str] = []
    for _ in range(5):
        ids.append(new_session_id())
        _time.sleep(0.003)  # 3 ms — comfortably above the 1 ms precision
    assert len(set(ids)) == len(ids)
    # And they really did share the same second (this isn't just a clock
    # rollover giving us distinct prefixes by accident).
    whole_second_prefixes = {sid.rsplit("-", 1)[0] for sid in ids}
    assert len(whole_second_prefixes) <= 2  # at most one second rollover


def test_sessions_root_honours_aidebate_home(sessions_dir: Path):
    # The sessions_dir fixture sets AIDEBATE_HOME to a tmp dir.
    expected = Path(os.environ["AIDEBATE_HOME"]) / "sessions"
    assert sessions_root() == expected


def test_sessions_root_default_without_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("AIDEBATE_HOME", raising=False)
    root = sessions_root()
    assert root == Path.home() / ".aidebate" / "sessions"
    # Must not contain spaces — AI agents emit unquoted shell commands
    # that shell-split on spaces and cause subtle failures.
    assert " " not in str(root)
