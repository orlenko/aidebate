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
    assert [(k.delay, k.key) for k in a.startup_keys] == [(1.0, "Space"), (2.5, "Enter")]


def test_missing_adapter_raises():
    with pytest.raises(FileNotFoundError):
        load_adapter("nope-not-real")
