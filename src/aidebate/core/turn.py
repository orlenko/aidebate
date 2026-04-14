"""Inject a prompt into an agent pane and wait for its .done flag."""
from __future__ import annotations

import time
from pathlib import Path

from .pane import AgentPane
from .watch import wait_for_file


def _prompt_with_instructions(body: str, answer_path: Path, done_path: Path, adapter_instr: str) -> str:
    instr = adapter_instr.format(
        answer_path=str(answer_path),
        done_path=str(done_path),
    )
    return f"{body}\n\n---\n\n{instr}"


def run_turn(
    agent: AgentPane,
    turn_dir: Path,
    prompt_body: str,
    timeout: float = 600.0,
) -> str:
    """Write a prompt file, tell the agent to execute it, wait for .done, return answer text."""
    turn_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = turn_dir / f"{agent.role}.prompt.md"
    answer_path = turn_dir / f"{agent.role}.answer.md"
    done_path = turn_dir / f"{agent.role}.done"

    full_prompt = _prompt_with_instructions(
        prompt_body, answer_path, done_path, agent.adapter.answer_instruction
    )
    prompt_path.write_text(full_prompt)

    # Tell the agent to read and follow the prompt file. One-line command
    # avoids paste timing issues and multi-line quoting.
    trigger = (
        f"Read the file {prompt_path} and follow its instructions exactly. "
        f"The instructions include how to signal completion."
    )
    # Give the REPL time to finish rendering any prior response before we
    # paste a new prompt. 1s was too tight for gemini; it would still be
    # redrawing post-canary output and swallow our paste.
    time.sleep(3.0)
    # Dismiss any lingering permission dialogs BEFORE we paste, so our
    # text lands in the real input box instead of a dialog.
    agent.handle_permission_prompts(duration=2.0)
    agent.send_text(trigger)
    # And again after — new prompts can appear in response to the paste.
    agent.handle_permission_prompts(duration=3.0)

    if not wait_for_file(done_path, timeout=timeout):
        last = agent.capture(lines=80)
        raise TimeoutError(
            f"Agent {agent.role} did not produce {done_path.name} in {timeout}s.\n"
            f"Last pane output:\n{last}"
        )

    if not answer_path.exists():
        raise RuntimeError(
            f"Agent {agent.role} touched .done but did not write {answer_path.name}."
        )
    return answer_path.read_text()


def canary_handshake(agent: AgentPane, timeout: float = 120.0) -> None:
    """Prove the pane works end-to-end: write to a file, touch .done."""
    canary_dir = agent.cwd / ".canary"
    canary_dir.mkdir(exist_ok=True)
    ready = canary_dir / "ready"
    done = canary_dir / "ready.done"
    # Clean prior attempts.
    for p in (ready, done):
        if p.exists():
            p.unlink()

    body = (
        "CANARY HANDSHAKE: This is a startup test. Write the single word 'ok' "
        f"to '{ready}', then touch '{done}'. Do not do anything else."
    )
    # Wait for the CLI banner to settle AND for any adapter startup_keys
    # (e.g. "press Enter to dismiss trust dialog") to have fired and the
    # dialog to have cleared. Otherwise the canary text gets typed into
    # the startup dialog instead of the real prompt.
    max_startup_delay = max(
        (sk.delay for sk in agent.adapter.startup_keys), default=0.0
    )
    # Generous minimum so the CLI is truly idle at its input prompt before
    # we paste. Claude Code's cold start can take 5+ seconds on a fresh
    # pane; 4s was marginal and one pane in three would miss the canary.
    # we paste into it. Claude Code in particular loads async and can miss
    # input sent too early.
    time.sleep(max(max_startup_delay + 2.5, 6.0))
    # Dismiss any startup dialogs (trust folder, workspace-add, etc.) that
    # appeared during the boot window, BEFORE we paste canary text.
    agent.handle_permission_prompts(duration=3.0)
    agent.send_text(body)
    agent.handle_permission_prompts(duration=5.0)

    if not wait_for_file(done, timeout=timeout):
        last = agent.capture(lines=120)
        raise TimeoutError(
            f"Canary handshake failed for {agent.role}. "
            f"Agent did not touch {done} within {timeout}s.\n\n"
            f"Last pane output:\n{last}"
        )
