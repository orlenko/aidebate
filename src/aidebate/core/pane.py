"""Thin wrapper around a tmux pane running a single agent."""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import libtmux

from .adapter import Adapter


@dataclass
class AgentPane:
    role: str
    adapter: Adapter
    pane: libtmux.Pane
    cwd: Path

    def send_text(self, text: str, submit_delay: float | None = None) -> None:
        """Type literal text into the pane, pause briefly, then press Enter.

        The pause matters: some TUIs (codex in particular) treat tmux's
        `send-keys -l` as a bracketed paste and keep absorbing input for a
        moment after the text arrives. If Enter lands during that window,
        it becomes a newline inside the draft instead of a submit. A short
        sleep between text and Enter makes the submission reliable.
        """
        delay = submit_delay if submit_delay is not None else self.adapter.submit_delay
        self.pane.send_keys(text, enter=False, literal=True)
        if delay > 0:
            time.sleep(delay)
        self.send_enter()

    def send_enter(self) -> None:
        """Press the Enter (C-m) key in the pane."""
        self.pane.cmd("send-keys", "Enter")

    def send_key(self, key: str) -> None:
        """Send a named tmux key like 'Enter', 'C-c', 'y'."""
        self.pane.cmd("send-keys", key)

    def capture(self, lines: int = 200) -> str:
        out = self.pane.cmd("capture-pane", "-p", "-S", f"-{lines}").stdout
        return "\n".join(out)

    def handle_permission_prompts(self, duration: float = 2.0) -> None:
        """Scan the pane briefly and auto-respond to known prompts.

        `respond` may be an empty string — in that case we just press Enter,
        which accepts the currently-highlighted choice in radio-style
        dialogs (e.g. gemini's trust folder prompt).
        """
        deadline = time.time() + duration
        responded_to: set[str] = set()
        while time.time() < deadline:
            text = self.capture(lines=80)
            for pat in self.adapter.permission_prompts:
                if pat.match.search(text) and pat.match.pattern not in responded_to:
                    if pat.respond:
                        self.pane.send_keys(pat.respond, enter=False, literal=True)
                        time.sleep(0.2)
                    self.send_enter()
                    responded_to.add(pat.match.pattern)
                    time.sleep(0.3)
            time.sleep(0.2)
