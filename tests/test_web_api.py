"""Smoke tests for the FastAPI layer.

These hit endpoints that don't need real tmux / CLI agents — listing
adapters, listing sessions on disk, reading one session's artifacts.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from aidebate.web.server import app


def test_adapters_endpoint_lists_shipped_yaml():
    client = TestClient(app)
    r = client.get("/api/adapters")
    assert r.status_code == 200
    names = r.json()
    assert {"claude", "gemini", "codex"}.issubset(set(names))


def test_sessions_list_empty(sessions_dir: Path):
    # sessions_dir fixture redirects AIDEBATE_HOME to tmp, so no sessions yet.
    client = TestClient(app)
    r = client.get("/api/sessions")
    assert r.status_code == 200
    assert r.json() == []


def test_sessions_list_reads_manifest(sessions_dir: Path):
    sid = "2026-04-14-120000"
    sdir = sessions_dir / sid
    sdir.mkdir(parents=True)
    manifest = {
        "session_id": sid,
        "topic": "Unit test topic",
        "moderator_agent": "claude",
        "sides": [{"role": "pro", "agent": "claude", "stance": "yes"}],
        "created_at": "2026-04-14T12:00:00",
        "status": "done",
        "verdict_path": "verdict.md",
        "completed_at": "2026-04-14T12:05:00",
    }
    (sdir / "session.json").write_text(json.dumps(manifest))
    (sdir / "verdict.md").write_text("# Winner: pro\n")

    client = TestClient(app)
    r = client.get("/api/sessions")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["session_id"] == sid
    assert data[0]["topic"] == "Unit test topic"
    assert data[0]["status"] == "done"


def test_session_detail_includes_verdict_and_phases(sessions_dir: Path):
    sid = "2026-04-14-130000"
    sdir = sessions_dir / sid
    (sdir / "phase-1-opening").mkdir(parents=True)
    (sdir / "phase-1-opening" / "pro.answer.md").write_text("pro says yes")
    (sdir / "phase-1-opening" / "con.answer.md").write_text("con says no")
    (sdir / "verdict.md").write_text("# Winner: pro\n")
    (sdir / "chat.jsonl").write_text('{"ts":"t","from":"pro","to":["*"],"text":"hi"}\n')

    client = TestClient(app)
    r = client.get(f"/api/sessions/{sid}")
    assert r.status_code == 200
    data = r.json()
    assert data["session_id"] == sid
    assert data["verdict"].startswith("# Winner")
    assert "phase-1-opening" in data["phases"]
    assert data["phases"]["phase-1-opening"]["pro"] == "pro says yes"
    assert len(data["chat"]) == 1
    assert data["chat"][0]["from"] == "pro"


def test_session_detail_404_for_unknown(sessions_dir: Path):
    client = TestClient(app)
    r = client.get("/api/sessions/does-not-exist")
    assert r.status_code == 404
