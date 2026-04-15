"""Exercise the pure prompt-builder helpers in aidebate.core.debate."""
from __future__ import annotations

from pathlib import Path

from aidebate.core.debate import (
    Side,
    _opening_prompt,
    _rebuttal_prompt,
    _roast_prompt,
    _verdict_prompt,
)


def _sides() -> list[Side]:
    return [
        Side(role="pro", stance="it is great", agent="claude"),
        Side(role="con", stance="it is bad", agent="gemini"),
    ]


def test_opening_prompt_mentions_role_stance_topic_and_chat(tmp_path: Path):
    sides = _sides()
    chat = tmp_path / "chat.jsonl"
    chat.touch()
    text = _opening_prompt(
        topic="Is it great?",
        side=sides[0],
        chat_path=chat,
        all_roles=[s.role for s in sides],
    )
    assert "Is it great?" in text
    assert "it is great" in text
    assert "pro" in text
    # Other debaters should be listed.
    assert "con" in text
    assert str(chat) in text


def test_rebuttal_prompt_includes_opponents_openings(tmp_path: Path):
    sides = _sides()
    openings = {"pro": "pro's opening", "con": "con's opening"}
    text = _rebuttal_prompt(
        topic="X",
        side=sides[0],
        openings=openings,
        chat_path=tmp_path / "chat.jsonl",
        chat_transcript="",
    )
    # pro sees con's opening, not their own.
    assert "con's opening" in text
    assert "pro's opening" not in text


def test_verdict_prompt_includes_every_submission(tmp_path: Path):
    sides = _sides()
    openings = {"pro": "O-pro", "con": "O-con"}
    rebuttals = {"pro": "R-pro", "con": "R-con"}
    text = _verdict_prompt(
        topic="X",
        sides=sides,
        openings=openings,
        rebuttals=rebuttals,
        chat_path=tmp_path / "chat.jsonl",
        chat_transcript="",
    )
    for needle in ("O-pro", "O-con", "R-pro", "R-con"):
        assert needle in text
    # Moderator must be asked to pick a winner.
    assert "Winner" in text


def test_verdict_prompt_notes_dropouts(tmp_path: Path):
    sides = [Side(role="pro", stance="yes", agent="claude")]  # only survivor
    text = _verdict_prompt(
        topic="X",
        sides=sides,
        openings={"pro": "opening"},
        rebuttals={},
        chat_path=tmp_path / "chat.jsonl",
        chat_transcript="",
        dropouts=[
            {
                "role": "con",
                "agent": "gemini",
                "phase": "canary",
                "error": "auth expired",
                "at": "2026-04-14T12:00:00",
            },
        ],
    )
    assert "dropped out" in text.lower() or "dropped" in text
    assert "con" in text
    assert "auth expired" in text
    # Moderator must be told not to penalize survivors for the dropouts.
    assert "do NOT penalize" in text or "do not penalize" in text.lower()


def test_verdict_prompt_no_dropout_section_when_empty(tmp_path: Path):
    text = _verdict_prompt(
        topic="X",
        sides=_sides(),
        openings={"pro": "O-pro", "con": "O-con"},
        rebuttals={"pro": "R-pro", "con": "R-con"},
        chat_path=tmp_path / "chat.jsonl",
        chat_transcript="",
        dropouts=[],
    )
    # When no dropouts, the "Participants who dropped out" section should
    # not appear at all — moderator shouldn't have to reason about an
    # empty absentee list.
    assert "dropped out" not in text


def test_roast_prompt_includes_everyone_and_transcript():
    sides = _sides()
    text = _roast_prompt(
        topic="X",
        sides=sides,
        openings={"pro": "O-pro", "con": "O-con"},
        rebuttals={"pro": "R-pro", "con": "R-con"},
        verdict_text="V-final",
        chat_transcript="chat-lines",
        moderator_agent="claude",
    )
    # Every submission + the verdict must be in the prompt.
    for needle in ("O-pro", "O-con", "R-pro", "R-con", "V-final", "chat-lines"):
        assert needle in text
    # Every participant, including the moderator, must be listed.
    assert "pro" in text and "con" in text and "moderator" in text
    # Roastmaster is told to follow CLAUDE.md (i.e. the tone sits in the
    # per-agent working directory, not re-pasted into every prompt).
    assert "CLAUDE.md" in text
