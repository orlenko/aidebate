"""tmux session lifecycle for a debate."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import libtmux
import platformdirs

from .adapter import Adapter
from .pane import AgentPane


def sessions_root() -> Path:
    """Where per-debate artifacts live on disk.

    Resolution order:
      1. AIDEBATE_HOME env var → <AIDEBATE_HOME>/sessions/
      2. platformdirs user_data_dir — on macOS this is
         ~/Library/Application Support/aidebate/sessions.

    Creating the directory is the caller's responsibility.
    """
    override = os.environ.get("AIDEBATE_HOME")
    base = (
        Path(override).expanduser()
        if override
        else Path(platformdirs.user_data_dir("aidebate"))
    )
    return base / "sessions"


def new_session_id() -> str:
    return datetime.now().strftime("%Y-%m-%d-%H%M%S")


@dataclass
class DebateSession:
    session_id: str
    root: Path  # sessions/<id>/
    tmux_session: libtmux.Session
    panes: dict[str, AgentPane] = field(default_factory=dict)

    @property
    def chat_path(self) -> Path:
        return self.root / "chat.jsonl"

    def pane_for(self, role: str) -> AgentPane:
        return self.panes[role]

    def kill(self) -> None:
        try:
            self.tmux_session.kill()
        except Exception:
            pass


def create_session(session_id: str | None = None) -> DebateSession:
    sid = session_id or new_session_id()
    root = sessions_root() / sid
    root.mkdir(parents=True, exist_ok=True)
    (root / "chat.jsonl").touch()

    server = libtmux.Server()
    tmux_name = f"debate-{sid}"
    # Kill any stale session with the same name.
    existing = server.sessions.filter(session_name=tmux_name)
    for s in existing:
        s.kill()
    # Size the virtual terminal generously. Detached tmux defaults to 80x24,
    # which makes every pane capture look cramped in the browser. These
    # dimensions are the session's logical size; real clients that attach
    # interactively will still drive their own size.
    tmux = server.new_session(
        session_name=tmux_name,
        kill_session=False,
        attach=False,
        window_name="debate",
        x=240,
        y=60,
    )
    return DebateSession(session_id=sid, root=root, tmux_session=tmux)


def spawn_agent_pane(
    session: DebateSession,
    role: str,
    adapter: Adapter,
) -> AgentPane:
    """Spawn a tmux *pane* (split) for this agent in the single debate window.

    The first agent reuses the window's initial pane; later agents each get
    a split. After each spawn we re-apply the `tiled` layout so panes stay
    evenly sized as more agents join.
    """
    agent_cwd = session.root / "agents" / role
    agent_cwd.mkdir(parents=True, exist_ok=True)

    window = session.tmux_session.active_window
    if not session.panes:
        # First agent: take over the window's initial pane.
        pane = window.active_pane
        pane.send_keys(f"cd '{agent_cwd}'", enter=True)
    else:
        # Subsequent agents: split.
        pane = window.split(
            attach=False,
            start_directory=str(agent_cwd),
        )
        window.select_layout("tiled")

    # Set a per-pane user option with the role. Why @role instead of the
    # built-in pane_title: Claude Code (and other CLIs) emit OSC escape
    # sequences to update the terminal title, which tmux treats as the
    # pane_title. Using a user option gives us a label tmux won't overwrite.
    label = f"{role} ({adapter.name})"
    try:
        pane.cmd("set-option", "-p", "@role", label)
    except Exception:
        pass

    # Launch the agent CLI. Allow adapter cmd to reference per-session paths.
    launch_cmd = adapter.cmd.format(
        session_root=str(session.root),
        agent_cwd=str(agent_cwd),
    )
    pane.send_keys(launch_cmd, enter=True)

    ap = AgentPane(role=role, adapter=adapter, pane=pane, cwd=agent_cwd)
    session.panes[role] = ap

    # Fire any startup key presses in a background thread so parallel panes
    # don't block each other. Used e.g. to dismiss gemini's "trust folder"
    # dialog whose default is already what we want.
    if adapter.startup_keys:
        import threading
        import time as _time

        def _fire_startup() -> None:
            for sk in adapter.startup_keys:
                _time.sleep(sk.delay)
                try:
                    pane.cmd("send-keys", sk.key)
                except Exception:
                    pass

        threading.Thread(target=_fire_startup, daemon=True).start()

    return ap


def enable_pane_titles(session: DebateSession) -> None:
    """Show each pane's role label in its top border."""
    try:
        session.tmux_session.cmd("set", "-g", "pane-border-status", "top")
        # Reference our user option so terminal-emitted titles can't clobber it.
        session.tmux_session.cmd("set", "-g", "pane-border-format", " #{@role} ")
    except Exception:
        pass


def apply_moderator_layout(session: DebateSession, moderator_width_pct: int = 33) -> None:
    """Give the moderator pane (assumed leftmost/first) a prominent share.

    Uses tmux's built-in `main-vertical` layout: one big main pane on the
    left, the rest stacked on the right. Moderator must already be the main
    (first) pane of the window.
    """
    try:
        win = session.tmux_session.active_window
        # `main-pane-width` can be a percentage string on modern tmux.
        win.cmd("set-option", "-w", "main-pane-width", f"{moderator_width_pct}%")
        win.select_layout("main-vertical")
    except Exception:
        # Fall back to tiled silently; not critical.
        try:
            session.tmux_session.active_window.select_layout("tiled")
        except Exception:
            pass
