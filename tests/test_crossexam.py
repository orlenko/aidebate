"""End-to-end tests for run_crossexam with stubbed agent turns.

The tmux/CLI side of the debate is project policy for manual smoke, not
pytest. But `run_crossexam` is the most concurrent code in aidebate —
worker threads, chat watcher, silence watcher, seed-vs-nudge dispatch,
termination by wallclock or silence — and tying its correctness only to
manual smoke is how regressions slip in.

These tests stub the one function that actually touches a real pane
(`run_turn`) and exercise everything else for real: real threads, real
file I/O, real queues, real termination conditions.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from aidebate.core import crossexam
from aidebate.core.events import EventLog


@dataclass
class StubPane:
    role: str
    cwd: Path


def _append_chat(chat_path: Path, msg: dict) -> None:
    """Simulate an agent invoking the chat-say helper."""
    with open(chat_path, "a") as f:
        f.write(json.dumps(msg) + "\n")


def _prepare_cwds(tmp_path: Path, roles: tuple[str, ...]) -> None:
    for r in roles:
        (tmp_path / r).mkdir(parents=True, exist_ok=True)


def _wait_until(predicate, timeout: float = 2.0, interval: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_seeds_all_roles_and_terminates_on_silence(tmp_path, monkeypatch):
    """Every role gets turn-001; the phase returns cleanly when chat stays quiet."""
    turn_calls: list[tuple[str, str]] = []

    def stub_run_turn(agent, turn_dir, prompt_body, timeout):
        turn_dir.mkdir(parents=True, exist_ok=True)
        turn_calls.append((agent.role, turn_dir.name))

    monkeypatch.setattr(crossexam, "run_turn", stub_run_turn)

    chat_path = tmp_path / "chat.jsonl"
    chat_path.touch()
    _prepare_cwds(tmp_path, ("moderator", "alpha", "beta"))

    phase_dir = crossexam.run_crossexam(
        session_root=tmp_path,
        chat_path=chat_path,
        moderator=StubPane("moderator", tmp_path / "moderator"),
        debaters={
            "alpha": StubPane("alpha", tmp_path / "alpha"),
            "beta": StubPane("beta", tmp_path / "beta"),
        },
        stances={"alpha": "pro", "beta": "con"},
        topic="test topic",
        openings={"alpha": "open-alpha", "beta": "open-beta"},
        wallclock=5.0,
        silence_timeout=0.3,
        moderator_silence_nudge=10.0,  # don't stall-nudge during this test
        turn_timeout=5.0,
        event_log=EventLog(tmp_path / "events.jsonl"),
    )

    assert phase_dir == tmp_path / "phase-2-crossexam"
    seeded = {role for role, turn in turn_calls if turn == "turn-001"}
    assert seeded == {"moderator", "alpha", "beta"}


def test_chat_message_nudges_addressees(tmp_path, monkeypatch):
    """A targeted chat message triggers a nudge turn for the addressee."""
    turn_calls: list[tuple[str, str]] = []
    lock = threading.Lock()

    def stub_run_turn(agent, turn_dir, prompt_body, timeout):
        turn_dir.mkdir(parents=True, exist_ok=True)
        with lock:
            turn_calls.append((agent.role, turn_dir.name))

    monkeypatch.setattr(crossexam, "run_turn", stub_run_turn)

    chat_path = tmp_path / "chat.jsonl"
    chat_path.touch()
    _prepare_cwds(tmp_path, ("moderator", "alpha", "beta"))

    result: dict[str, Path] = {}

    def run() -> None:
        result["phase_dir"] = crossexam.run_crossexam(
            session_root=tmp_path,
            chat_path=chat_path,
            moderator=StubPane("moderator", tmp_path / "moderator"),
            debaters={
                "alpha": StubPane("alpha", tmp_path / "alpha"),
                "beta": StubPane("beta", tmp_path / "beta"),
            },
            stances={"alpha": "pro", "beta": "con"},
            topic="t",
            openings={"alpha": "o-a", "beta": "o-b"},
            wallclock=5.0,
            silence_timeout=0.5,
            moderator_silence_nudge=10.0,
            turn_timeout=5.0,
            event_log=EventLog(tmp_path / "events.jsonl"),
        )

    t = threading.Thread(target=run, daemon=True)
    t.start()

    # Wait for all three seed turns to register before injecting chat.
    assert _wait_until(
        lambda: (
            {role for role, turn in turn_calls if turn == "turn-001"}
            == {"moderator", "alpha", "beta"}
        ),
        timeout=3.0,
    ), "seed turns did not fire for all roles"

    # Alpha sends beta a targeted question → beta should receive a nudge turn.
    _append_chat(
        chat_path,
        {
            "ts": "2026-04-16T20:00:00Z",
            "from": "alpha",
            "to": ["beta"],
            "text": "What's your best counter-argument?",
        },
    )

    t.join(timeout=10.0)
    assert not t.is_alive(), "run_crossexam did not terminate within 10s"

    beta_turns = [turn for role, turn in turn_calls if role == "beta"]
    assert "turn-001" in beta_turns  # seed
    assert "turn-002" in beta_turns  # nudge triggered by alpha's message

    # The chat watcher should have emitted a chat_message event.
    events_content = (tmp_path / "events.jsonl").read_text()
    assert '"chat_message"' in events_content
    assert '"alpha"' in events_content


def test_broadcast_message_nudges_everyone_except_sender(tmp_path, monkeypatch):
    """A `to: ['*']` message nudges every role except the sender."""
    turn_calls: list[tuple[str, str]] = []
    lock = threading.Lock()

    def stub_run_turn(agent, turn_dir, prompt_body, timeout):
        turn_dir.mkdir(parents=True, exist_ok=True)
        with lock:
            turn_calls.append((agent.role, turn_dir.name))

    monkeypatch.setattr(crossexam, "run_turn", stub_run_turn)

    chat_path = tmp_path / "chat.jsonl"
    chat_path.touch()
    _prepare_cwds(tmp_path, ("moderator", "alpha", "beta"))

    def run() -> None:
        crossexam.run_crossexam(
            session_root=tmp_path,
            chat_path=chat_path,
            moderator=StubPane("moderator", tmp_path / "moderator"),
            debaters={
                "alpha": StubPane("alpha", tmp_path / "alpha"),
                "beta": StubPane("beta", tmp_path / "beta"),
            },
            stances={"alpha": "pro", "beta": "con"},
            topic="t",
            openings={"alpha": "o-a", "beta": "o-b"},
            wallclock=5.0,
            silence_timeout=0.5,
            moderator_silence_nudge=10.0,
            turn_timeout=5.0,
            event_log=EventLog(tmp_path / "events.jsonl"),
        )

    t = threading.Thread(target=run, daemon=True)
    t.start()

    assert _wait_until(
        lambda: (
            {role for role, turn in turn_calls if turn == "turn-001"}
            == {"moderator", "alpha", "beta"}
        ),
        timeout=3.0,
    )

    _append_chat(
        chat_path,
        {
            "ts": "2026-04-16T20:00:10Z",
            "from": "alpha",
            "to": ["*"],
            "text": "hot take incoming",
        },
    )

    t.join(timeout=10.0)
    assert not t.is_alive()

    # Everyone except alpha (the sender) should have advanced past turn-001.
    for role in ("moderator", "beta"):
        role_turns = [turn for r, turn in turn_calls if r == role]
        assert "turn-002" in role_turns, f"{role} did not get a nudge turn"

    alpha_turns = [turn for r, turn in turn_calls if r == "alpha"]
    assert "turn-002" not in alpha_turns, "alpha should not nudge itself"


def test_installs_chat_say_helpers(tmp_path, monkeypatch):
    """run_crossexam writes an executable chat-say script into each agent cwd."""

    def stub_run_turn(agent, turn_dir, prompt_body, timeout):
        turn_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(crossexam, "run_turn", stub_run_turn)

    chat_path = tmp_path / "chat.jsonl"
    chat_path.touch()
    _prepare_cwds(tmp_path, ("moderator", "alpha"))

    crossexam.run_crossexam(
        session_root=tmp_path,
        chat_path=chat_path,
        moderator=StubPane("moderator", tmp_path / "moderator"),
        debaters={"alpha": StubPane("alpha", tmp_path / "alpha")},
        stances={"alpha": "pro"},
        topic="t",
        openings={"alpha": "o"},
        wallclock=3.0,
        silence_timeout=0.3,
        moderator_silence_nudge=10.0,
        turn_timeout=5.0,
        event_log=EventLog(tmp_path / "events.jsonl"),
    )

    for role in ("moderator", "alpha"):
        helper = tmp_path / role / "chat-say"
        assert helper.exists(), f"chat-say helper missing for {role}"
        assert helper.stat().st_mode & 0o111, "chat-say helper not executable"
