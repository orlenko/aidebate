"""Adapter YAML loading + startup-key parsing."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from aidebate.core.adapter import ADAPTERS_DIR, Adapter, load_adapter


def test_shipped_adapters_load():
    """Every adapter that ships with the package must load cleanly."""
    for p in sorted(ADAPTERS_DIR.glob("*.yaml")):
        a = load_adapter(p.stem)
        assert a.name
        assert a.cmd
        assert a.submit_delay >= 0
        assert isinstance(a.permission_prompts, list)
        assert isinstance(a.ready_patterns, list)
        assert isinstance(a.startup_keys, list)


def test_claude_adapter_shape():
    a = load_adapter("claude")
    assert a.name == "claude"
    assert "--dangerously-skip-permissions" in a.cmd
    # answer_instruction must quote the done_path so role names with spaces work.
    assert "'{done_path}'" in a.answer_instruction


def test_gemini_adapter_has_two_startup_enters():
    """Gemini's boot sequence has two sequential dialogs; both must be dismissed."""
    a = load_adapter("gemini")
    assert len(a.startup_keys) == 2
    assert all(k.key == "Enter" for k in a.startup_keys)
    delays = sorted(k.delay for k in a.startup_keys)
    assert delays[1] > delays[0]


def test_gemini_adapter_has_ready_pattern():
    a = load_adapter("gemini")
    assert any(p.search("Type your message or @path/to/file") for p in a.ready_patterns)


def test_gemini_trust_dialog_matches_one_permission_pattern():
    a = load_adapter("gemini")
    dialog = "Do you trust the files in this folder?\n1. Trust folder\n2. Don't trust"
    matches = [p for p in a.permission_prompts if p.match.search(dialog)]
    assert len(matches) == 1
    assert matches[0].respond == ""


def test_custom_adapter_round_trip(tmp_path: Path):
    """Round-trip a hand-written adapter to make sure the loader is complete."""
    yaml_text = textwrap.dedent(
        """
        name: demo
        cmd: "echo {session_root}"
        submit_key: Enter
        submit_delay: 0.5
        permission_prompts:
          - { match: "yes/no", respond: "y" }
        answer_instruction: |
          Write to {answer_path} and touch '{done_path}'.
        startup_keys:
          - { delay: 1.0, key: Space }
          - { delay: 2.5, key: Enter }
        """
    ).strip()
    p = tmp_path / "demo.yaml"
    p.write_text(yaml_text)
    a = Adapter.load(p)
    assert a.name == "demo"
    assert a.submit_delay == 0.5
    assert len(a.permission_prompts) == 1
    assert a.permission_prompts[0].match.search("yes/no") is not None
    assert a.ready_patterns == []
    assert [(k.delay, k.key) for k in a.startup_keys] == [(1.0, "Space"), (2.5, "Enter")]


def test_missing_adapter_raises():
    with pytest.raises(FileNotFoundError):
        load_adapter("nope-not-real")


def test_gemini_cmd_quotes_session_root():
    """Sessions path may contain spaces (macOS user data dir does).

    Gemini's cmd must quote the {session_root} placeholder so a spaced
    path like '~/Library/Application Support/aidebate/sessions/...'
    doesn't get shell-split into two arguments.
    """
    a = load_adapter("gemini")
    formatted = a.cmd.format(
        session_root="/Users/vorlenko/Library/Application Support/aidebate/sessions/s",
        agent_cwd="/Users/vorlenko/Library/Application Support/aidebate/sessions/s/agents/pro",
    )
    # Every substituted path must live inside quotes.
    assert "'/Users/vorlenko/Library/Application Support" in formatted
    # No bare space outside quotes in the substituted region.
    assert "--include-directories '/Users/" in formatted
