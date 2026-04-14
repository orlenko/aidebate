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


def test_session_marked_stale_when_running_but_no_live_state(sessions_dir: Path):
    """Manifest says running but nothing in the in-memory SESSIONS dict
    (server restarted mid-run) — list should downgrade it to 'stale'.
    """
    sid = "2026-04-14-200000"
    sdir = sessions_dir / sid
    sdir.mkdir(parents=True)
    (sdir / "session.json").write_text(json.dumps({
        "session_id": sid,
        "topic": "X?",
        "moderator_agent": "claude",
        "sides": [],
        "status": "running",
        "created_at": "2026-04-14T20:00:00",
    }))
    client = TestClient(app)
    r = client.get("/api/sessions")
    assert r.status_code == 200
    entry = next((e for e in r.json() if e["session_id"] == sid), None)
    assert entry is not None
    assert entry["status"] == "stale"


def test_create_debate_walks_import_path(sessions_dir: Path, monkeypatch):
    """POST /api/debates should not explode on ModuleNotFoundError-style bugs.

    We stub out run_debate and DebateSession.kill to avoid touching tmux,
    then POST a valid payload. The handler imports `new_session_id` lazily,
    so this exercises the import without actually spawning panes.
    """
    import aidebate.web.server as server

    # Make the debate thread a no-op that just signals ready.
    class _FakeSession:
        def __init__(self, sid):
            self.session_id = sid
            from pathlib import Path as _P
            self.root = _P(sessions_dir) / sid
            self.root.mkdir(parents=True, exist_ok=True)
            self.panes = {}
        def kill(self): pass

    def _fake_run(**kwargs):
        sid = "2026-04-14-160000"
        fs = _FakeSession(sid)
        cb = kwargs.get("on_session_ready")
        if cb:
            cb(fs)
        return fs

    monkeypatch.setattr(server, "run_debate", _fake_run)

    client = TestClient(app)
    r = client.post(
        "/api/debates",
        json={
            "topic": "is X better than Y?",
            "moderator": "claude",
            "sides": [
                {"role": "pro", "agent": "claude", "stance": "yes"},
                {"role": "con", "agent": "claude", "stance": "no"},
            ],
        },
    )
    assert r.status_code == 200, r.text
    assert "session_id" in r.json()
