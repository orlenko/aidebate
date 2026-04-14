"""Helpers for surfacing a detached tmux session to the user."""
from __future__ import annotations

import os
import platform
import subprocess
import sys


def attach_command(session_name: str) -> str:
    return f"tmux attach -t {session_name}"


def _osascript(script: str) -> bool:
    try:
        subprocess.run(["osascript", "-e", script], check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def _open_iterm2(cmd: str) -> bool:
    # iTerm2's modern AppleScript API: create window w/ default profile,
    # then run the command in its current session.
    script = f'''
tell application "iTerm"
    activate
    set newWin to (create window with default profile)
    tell current session of newWin
        write text "{cmd}"
    end tell
end tell
'''
    return _osascript(script)


def _open_terminal_app(cmd: str) -> bool:
    ok = _osascript(f'tell application "Terminal" to do script "{cmd}"')
    if ok:
        _osascript('tell application "Terminal" to activate')
    return ok


def open_in_new_terminal(session_name: str) -> bool:
    """Open a new OS terminal window already attached to the tmux session.

    Prefers iTerm2 if the user is already running it ($TERM_PROGRAM =
    iTerm.app), otherwise falls back to Terminal.app. macOS only; other
    platforms return False and the caller falls back to a manual attach.
    """
    cmd = attach_command(session_name)
    if platform.system() != "Darwin":
        return False
    term = os.environ.get("TERM_PROGRAM", "")
    if term == "iTerm.app":
        if _open_iterm2(cmd):
            return True
        # Fall through to Terminal.app if iTerm AppleScript fails.
    return _open_terminal_app(cmd)


def wait_for_user(session_name: str) -> None:
    """Print attach instructions and block until the user hits Enter."""
    print()
    print("  To watch the debate live, open another terminal and run:")
    print(f"      {attach_command(session_name)}")
    print()
    if sys.stdin.isatty():
        try:
            input("  Press Enter once attached to begin... ")
        except (EOFError, KeyboardInterrupt):
            print()
