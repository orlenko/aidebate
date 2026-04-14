"""aidebate — multi-agent AI debate orchestrator.

Spawns a tmux session with one pane per AI CLI agent (Claude Code, Gemini,
Codex, …), coordinates debates via flag files, and optionally exposes a
FastAPI + SSE web UI for live viewing and archive browsing.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("aidebate")
except PackageNotFoundError:  # editable / dev install
    __version__ = "0+unknown"

__all__ = ["__version__"]
