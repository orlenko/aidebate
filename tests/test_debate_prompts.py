"""Exercise the pure prompt-builder helpers in aidebate.core.debate."""
from __future__ import annotations

from pathlib import Path

from aidebate.core.debate import (
    Side,
    _opening_prompt,
    _rebuttal_prompt,
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
    )
    for needle in ("O-pro", "O-con", "R-pro", "R-con"):
        assert needle in text
    # Moderator must be asked to pick a winner.
    assert "Winner" in text
