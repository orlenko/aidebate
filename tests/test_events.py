"""EventLog writer + read_events parser."""

from __future__ import annotations

from pathlib import Path

from aidebate.core.events import EventLog, read_events


def test_emit_appends_json_line(tmp_path: Path):
    log = EventLog(tmp_path / "events.jsonl")
    log.emit("phase_started", phase="opening")
    log.emit("chat_message", **{"from": "pro", "to": ["con"], "text": "hi"})

    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    assert len(lines) == 2
    events = read_events(tmp_path / "events.jsonl")
    assert events[0]["type"] == "phase_started"
    assert events[0]["phase"] == "opening"
    assert "ts" in events[0]
    assert events[1]["type"] == "chat_message"
    assert events[1]["from"] == "pro"
    assert events[1]["to"] == ["con"]


def test_read_events_missing_file_returns_empty(tmp_path: Path):
    assert read_events(tmp_path / "nope.jsonl") == []


def test_read_events_skips_malformed_lines(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"type":"phase_started","phase":"opening"}\n'
        "garbage not json\n"
        "\n"
        '{"type":"debate_completed","status":"done"}\n'
    )
    events = read_events(path)
    assert [e["type"] for e in events] == ["phase_started", "debate_completed"]
