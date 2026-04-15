---
description: Python conventions for aidebate — active when editing Python files
globs: ["**/*.py"]
---

# aidebate Python rules

## Setup & tools

- Python 3.10+ required. `src/aidebate/` is the package; `tests/` is the test suite.
- Local venv: `.venv/`. Run `source .venv/bin/activate` first, or use `.venv/bin/python` directly.
- Install with `pip install -e ".[dev]"` if deps are missing.
- Lint/format: `ruff check` and `ruff format` (config in `pyproject.toml`, line-length 100, target py310).
- Tests: `python -m pytest`. They're fast (~0.4s). Run them before claiming done. Always.

## Code style

- Type hints everywhere. `from __future__ import annotations` at the top of every module.
- Prefer `pathlib.Path` over `os.path`.
- Dataclasses for plain data structures (see `Side`, `SessionState`).
- One-line docstrings. No epic module-level prose.
- No comments restating what the code does. Comments explain *why* a non-obvious choice was made (e.g. "claude picks up CLAUDE.md from cwd at launch, so write it first").

## Don't mock these in tests

- **tmux / libtmux** — real behavior depends on real tmux.
- **adapters/*.yaml** — load the real ones; they're stable.
- **session filesystem layout** — use `tmp_path`.
- **subprocess/CLI agents** — any test touching a real agent pane belongs in manual smoke, not `pytest`.

Unit tests cover pure helpers: prompt builders, CLI argparse, session path helpers, web API routing. That's it.

## FastAPI / web

- One-process app: FastAPI + uvicorn, threads for debate workers, a per-session poller.
- SSE for live events (`/api/debates/{sid}/events`).
- Keep endpoints thin; business logic lives in `core/`.

## Debate pipeline (cheat sheet)

Phases run in order via `src/aidebate/core/debate.py::run_debate`:

1. canary handshakes (parallel) — fatal for moderator, non-fatal for debaters & roastmaster.
2. opening (parallel).
3. cross-examination (event-driven, see `core/crossexam.py`).
4. rebuttal (parallel).
5. verdict (moderator alone).
6. roast (roastmaster; skippable via `roast=False`).

Dropouts are recorded in `session.json` and surface in the web UI.
