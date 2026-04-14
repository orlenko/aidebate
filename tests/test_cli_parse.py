"""Side-spec parser and CLI parser smoke tests."""
from __future__ import annotations

import argparse

import pytest

from aidebate.cli import build_parser, parse_side
from aidebate.core.debate import Side


def test_parse_side_basic():
    s = parse_side("pro:argue for X")
    assert s == Side(role="pro", stance="argue for X", agent="claude")


def test_parse_side_with_agent_override():
    s = parse_side("pro@gemini:argue for X")
    assert s == Side(role="pro", stance="argue for X", agent="gemini")


def test_parse_side_respects_default_agent():
    s = parse_side("pro:argue for X", default_agent="codex")
    assert s == Side(role="pro", stance="argue for X", agent="codex")


def test_parse_side_stance_can_contain_colons():
    s = parse_side("pro:claim: that Rust wins on safety")
    assert s.stance == "claim: that Rust wins on safety"


@pytest.mark.parametrize(
    "spec",
    [
        "nocolon",
        ":nothing",
        "pro:",
        "pro@:stance",
        "@gemini:stance",
    ],
)
def test_parse_side_rejects_malformed(spec: str):
    with pytest.raises(argparse.ArgumentTypeError):
        parse_side(spec)


def test_build_parser_run_subcommand_minimal():
    p = build_parser()
    ns = p.parse_args(
        [
            "run",
            "--topic", "Is Python great?",
            "--side", "pro:yes",
            "--side", "con:no",
        ]
    )
    assert ns.cmd == "run"
    assert ns.topic == "Is Python great?"
    assert ns.side == ["pro:yes", "con:no"]
    assert ns.moderator == "claude"
    assert ns.default_agent == "claude"


def test_build_parser_sessions_dir_option():
    p = build_parser()
    ns = p.parse_args(["ls", "--sessions-dir", "/tmp/xx"])
    assert ns.sessions_dir == "/tmp/xx"
