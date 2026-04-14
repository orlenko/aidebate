# aidebate

Multi-agent AI debate orchestrator. Spins up **Claude Code**, **Gemini CLI**, **Codex CLI** (and anything else with an interactive REPL) inside tmux panes, runs a structured debate between them, and serves a live web UI plus an archive browser.

![status: alpha](https://img.shields.io/badge/status-alpha-orange) ![license: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue)

## Why

You have subscriptions to several AI coding assistants and occasionally want to pit them against each other — or let them cross-examine each other — on a concrete question ("Rust vs Go for this CLI", "Which of these 5 libraries should we pick?", "Is this migration worth it?"). Doing this manually is tedious; `aidebate` wires it up end to end:

- Each agent is a long-lived REPL in its own tmux pane (so you can watch them think live, or just see the final verdict).
- The orchestrator coordinates turns via **flag files** — agents never step on each other's keystrokes.
- A **moderator** agent runs the same way, reads everyone's answers at the end, and writes a verdict.
- Everything is persisted to disk as rendered Markdown, so every debate leaves a permanent, browsable transcript.

## Install

You need `tmux`, Python 3.10+, and at least one authenticated AI CLI:

- [Claude Code](https://claude.ai/code) — run `claude` once and log in.
- [Gemini CLI](https://github.com/google-gemini/gemini-cli) — run `gemini` once and sign in.
- [Codex CLI](https://github.com/openai/codex) — run `codex login`.

Then:

```sh
# One-shot (npx-style) with uv:
uvx aidebate serve

# Or persistent install:
pipx install aidebate
debate serve
```

Or develop from source:

```sh
git clone https://github.com/orlenko/aidebate && cd aidebate
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/debate --help
```

## Quickstart

### Web UI (recommended)

```sh
debate serve     # http://127.0.0.1:8765
```

Fill in the form — topic, moderator engine, and one row per debater (role + engine + stance). Click *Start*. The tiled tmux session spins up in the background and every pane streams into the browser. When it finishes, the verdict renders as proper Markdown and every run joins the "Prior debates" list for future reference.

### Command line

```sh
debate run \
  --topic "Should we migrate from Framework A to Framework B?" \
  --side pro@claude:"Argue for migrating." \
  --side con@gemini:"Argue for staying." \
  --side auditor@codex:"Neutral cost analysis — no advocacy." \
  --moderator claude \
  --watch
```

`--watch` pops open an iTerm2/Terminal window already attached to the debate's tmux session so you can follow along. Without it, use the printed `tmux attach -t debate-<timestamp>` command.

### Other subcommands

| Command                 | What it does                                               |
| ----------------------- | ---------------------------------------------------------- |
| `debate ls`             | List prior session IDs.                                    |
| `debate attach <id>`    | Print the `tmux attach` command for a session.             |
| `debate kill-all`       | Kill every tmux session whose name starts with `debate-`.  |
| `debate smoke`          | Walking-skeleton test: one agent, one turn, no moderator.  |

## What gets saved

Each run writes to `~/.aidebate/sessions/<id>/`. Override with `AIDEBATE_HOME=<dir>` or `--sessions-dir <dir>` on any subcommand. (We deliberately avoid macOS's `~/Library/Application Support/` path — the space breaks too many of the ad-hoc shell commands that AI agents emit mid-debate.)

```
~/.aidebate/sessions/2026-04-14-120000/
├── session.json                    # manifest: topic, sides, moderator, status, timings
├── chat.jsonl                      # group chat log (one JSON object per line)
├── verdict.md                      # moderator's final ruling
├── agents/
│   ├── pro/                        # each agent's cwd
│   ├── con/
│   └── moderator/
├── phase-1-opening/
│   ├── pro.prompt.md
│   ├── pro.answer.md
│   ├── pro.done
│   └── …
├── phase-2-rebuttal/
└── phase-3-verdict/
```

## How it works

Agents coordinate via **flag files**, not terminal scraping:

1. Orchestrator writes `phase-N/<role>.prompt.md`.
2. It tells the agent (via tmux `send-keys`) to read that file and follow it.
3. The prompt instructs the agent to write its response to `<role>.answer.md` and then `touch '<role>.done'`.
4. Orchestrator polls for the `.done` flag; when every agent's `.done` lands, the phase is complete.

The debate runs in three phases:

```
Phase 1: Opening    — each debater writes their position (parallel)
Phase 2: Rebuttal   — each debater attacks every opponent's opening (parallel)
Phase 3: Verdict    — moderator reads all answers + chat, renders ruling
```

`startup_keys` in the adapter config auto-dismiss "trust this folder?" dialogs for each CLI. `permission_prompts` handle anything else interactive.

## Adapters

Adding a new AI CLI is a YAML file under `src/aidebate/adapters/`:

```yaml
# src/aidebate/adapters/mynewcli.yaml
name: mynewcli
cmd: "mynewcli --yolo --workspace {session_root}"
submit_key: "Enter"
submit_delay: 0.8
permission_prompts:
  - { match: "Trust this", respond: "" }  # empty respond → just press Enter
  - { match: "\\[y/N\\]",  respond: "y" }
answer_instruction: |
  When you have finished this task, write your full response to:

      {answer_path}

  Then run:

      touch '{done_path}'
startup_keys:
  - { delay: 3.0, key: "Enter" }   # dismiss startup dialogs
```

The adapter's `cmd` can reference `{session_root}` and `{agent_cwd}`, substituted at spawn time.

## Development

```sh
.venv/bin/pytest                # run the test suite
.venv/bin/ruff check src tests  # lint
```

The tests don't require `tmux` or any AI CLI — they cover adapter loading, side parsing, prompt building, and the FastAPI endpoints that read session state from disk. For end-to-end runs you need the real thing.

## License

Apache-2.0. See [LICENSE](./LICENSE).
