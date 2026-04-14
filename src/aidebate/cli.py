"""Entry point for the `debate` console script (from the `aidebate` package).

Subcommands:
  run         Run a full debate (opening → rebuttal → verdict).
  smoke       Walking-skeleton test: one agent, canary + one turn.
  serve       Start the web UI.
  ls          List prior sessions on disk.
  attach ID   Print the tmux attach command for a session.
  kill-all    Kill every tmux session whose name starts with 'debate-'.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from aidebate import __version__
from aidebate.core.adapter import load_adapter
from aidebate.core.debate import Side, run_debate
from aidebate.core.session import (
    create_session,
    sessions_root,
    spawn_agent_pane,
)
from aidebate.core.turn import canary_handshake, run_turn
from aidebate.core.viewer import open_in_new_terminal, wait_for_user


def _apply_sessions_dir(args: argparse.Namespace) -> None:
    """If --sessions-dir was passed, honour it via AIDEBATE_HOME."""
    override = getattr(args, "sessions_dir", None)
    if override:
        os.environ["AIDEBATE_HOME"] = str(Path(override).expanduser().resolve())


def cmd_smoke(args: argparse.Namespace) -> int:
    _apply_sessions_dir(args)
    adapter = load_adapter(args.agent)
    session = create_session()
    tmux_name = f"debate-{session.session_id}"
    print(f"[debate] session_id={session.session_id}")
    print(f"[debate] spawning {adapter.name} pane...")
    agent = spawn_agent_pane(session, role="solo", adapter=adapter)

    if args.watch:
        if not open_in_new_terminal(tmux_name):
            print(f"[debate] couldn't auto-open a terminal; attach manually: tmux attach -t {tmux_name}")
    if args.no_wait:
        print(f"[debate] attach with: tmux attach -t {tmux_name}")
    else:
        wait_for_user(tmux_name)

    print(f"[debate] running canary handshake (timeout {args.canary_timeout}s)...")
    try:
        canary_handshake(agent, timeout=args.canary_timeout)
    except TimeoutError as e:
        print(f"[debate] CANARY FAILED\n{e}", file=sys.stderr)
        if not args.keep:
            session.kill()
        return 1
    print("[debate] canary OK")

    prompt = (
        f"Topic: {args.topic}\n\n"
        "Write a concise (~150 word) position paper on this topic. "
        "State your thesis, give your 3 strongest arguments, and acknowledge "
        "one legitimate counter-argument."
    )
    turn_dir = session.root / "turn-1"
    print(f"[debate] running turn-1 (timeout {args.turn_timeout}s)...")
    try:
        answer = run_turn(agent, turn_dir, prompt, timeout=args.turn_timeout)
    except (TimeoutError, RuntimeError) as e:
        print(f"[debate] TURN FAILED\n{e}", file=sys.stderr)
        if not args.keep:
            session.kill()
        return 1

    print("\n===== ANSWER =====\n")
    print(answer)
    print("\n===== END =====\n")
    print(f"[debate] artifacts in {session.root}")
    if not args.keep:
        session.kill()
        print("[debate] tmux session killed (pass --keep to leave it running)")
    return 0


def parse_side(spec: str, default_agent: str = "claude") -> Side:
    """Parse --side ROLE[@AGENT]:STANCE.

    Examples:
        pro:"argue for X"                     -> role=pro, agent=claude
        pro@gemini:"argue for X"              -> role=pro, agent=gemini
        rustacean@codex:"Rust wins on safety" -> role=rustacean, agent=codex
    """
    if ":" not in spec:
        raise argparse.ArgumentTypeError(
            f"--side must be ROLE[@AGENT]:STANCE, got: {spec!r}"
        )
    head, stance = spec.split(":", 1)
    head = head.strip()
    stance = stance.strip()
    if "@" in head:
        role, agent = head.split("@", 1)
        role, agent = role.strip(), agent.strip()
    else:
        role, agent = head, default_agent
    if not role or not stance or not agent:
        raise argparse.ArgumentTypeError(
            f"empty role/agent/stance in --side {spec!r}"
        )
    return Side(role=role, stance=stance, agent=agent)


def cmd_run(args: argparse.Namespace) -> int:
    _apply_sessions_dir(args)
    if len(args.side) < 2:
        print("[debate] need at least two --side entries", file=sys.stderr)
        return 2
    sides = [parse_side(s, default_agent=args.default_agent) for s in args.side]
    roles = [s.role for s in sides]
    if len(set(roles)) != len(roles) or "moderator" in roles:
        print("[debate] side roles must be unique and not 'moderator'", file=sys.stderr)
        return 2

    def _on_ready(session) -> None:
        tmux_name = f"debate-{session.session_id}"
        if args.watch:
            if not open_in_new_terminal(tmux_name):
                print(f"[debate] couldn't auto-open a terminal; attach manually: tmux attach -t {tmux_name}")
        if args.no_wait:
            print(f"[debate] attach with: tmux attach -t {tmux_name}")
        else:
            wait_for_user(tmux_name)

    try:
        session = run_debate(
            topic=args.topic,
            sides=sides,
            moderator_agent=args.moderator,
            canary_timeout=args.canary_timeout,
            turn_timeout=args.turn_timeout,
            crossexam_wallclock=args.crossexam_wallclock,
            crossexam_silence=args.crossexam_silence,
            on_session_ready=_on_ready,
        )
    except Exception as e:
        print(f"[debate] ABORTED: {e}", file=sys.stderr)
        return 1

    verdict_path = session.root / "verdict.md"
    print("\n===== VERDICT =====\n")
    print(verdict_path.read_text())
    print("\n===== END =====\n")
    print(f"[debate] artifacts in {session.root}")
    if not args.keep:
        session.kill()
        print("[debate] tmux session killed (pass --keep to leave it running)")
    return 0


def cmd_ls(args: argparse.Namespace) -> int:
    _apply_sessions_dir(args)
    root = sessions_root()
    if not root.exists():
        return 0
    for d in sorted(root.iterdir()):
        if d.is_dir():
            print(d.name)
    return 0


def cmd_attach(args: argparse.Namespace) -> int:
    print(f"tmux attach -t debate-{args.session_id}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    _apply_sessions_dir(args)
    from aidebate.web.server import serve

    serve(host=args.host, port=args.port)
    return 0


def cmd_kill_all(args: argparse.Namespace) -> int:
    import libtmux

    server = libtmux.Server()
    killed = 0
    for s in list(server.sessions):
        name = s.session_name or ""
        if name.startswith("debate-"):
            print(f"[debate] killing {name}")
            try:
                s.kill()
                killed += 1
            except Exception as e:
                print(f"[debate]   failed: {e}", file=sys.stderr)
    print(f"[debate] killed {killed} session(s)")
    return 0


def _add_sessions_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--sessions-dir",
        default=None,
        help="Override where session artifacts are stored. "
        "Defaults to the platform user data dir, or $AIDEBATE_HOME/sessions "
        "if that env var is set.",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="debate")
    p.add_argument("--version", action="version", version=f"aidebate {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("run", help="Run a full debate: opening -> rebuttal -> verdict.")
    sp.add_argument("--topic", required=True)
    sp.add_argument(
        "--side",
        action="append",
        default=[],
        required=True,
        metavar="ROLE[@AGENT]:STANCE",
        help="Debater. Pass at least twice.",
    )
    sp.add_argument(
        "--default-agent",
        default="claude",
        help="Adapter to use for sides that don't specify @AGENT (default: claude).",
    )
    sp.add_argument(
        "--moderator",
        default="claude",
        help="Adapter for the moderator pane (default: claude).",
    )
    sp.add_argument("--canary-timeout", type=float, default=180.0)
    sp.add_argument("--turn-timeout", type=float, default=900.0)
    sp.add_argument(
        "--crossexam-wallclock",
        type=float,
        default=300.0,
        help="Max duration of the cross-examination phase, seconds (default: 300).",
    )
    sp.add_argument(
        "--crossexam-silence",
        type=float,
        default=180.0,
        help="End cross-exam after this many seconds of chat silence (default: 180).",
    )
    sp.add_argument("--keep", action="store_true")
    sp.add_argument("--watch", action="store_true", help="Open a new terminal window attached to the session (macOS).")
    sp.add_argument("--no-wait", action="store_true", help="Don't pause for the user to attach before starting.")
    _add_sessions_dir(sp)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("smoke", help="Walking-skeleton test: one agent, canary + one turn.")
    sp.add_argument("--agent", default="claude", help="Adapter name (default: claude)")
    sp.add_argument("--topic", default="Rust vs Go for building small CLI tools.")
    sp.add_argument("--canary-timeout", type=float, default=120.0)
    sp.add_argument("--turn-timeout", type=float, default=600.0)
    sp.add_argument("--keep", action="store_true", help="Leave tmux session running after completion.")
    sp.add_argument("--watch", action="store_true", help="Open a new terminal window attached to the session.")
    sp.add_argument("--no-wait", action="store_true", help="Don't pause for the user to attach before starting.")
    _add_sessions_dir(sp)
    sp.set_defaults(func=cmd_smoke)

    sp = sub.add_parser("serve", help="Start the web UI.")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
    _add_sessions_dir(sp)
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("ls", help="List sessions.")
    _add_sessions_dir(sp)
    sp.set_defaults(func=cmd_ls)

    sp = sub.add_parser("attach", help="Print tmux attach command.")
    sp.add_argument("session_id")
    sp.set_defaults(func=cmd_attach)

    sp = sub.add_parser("kill-all", help="Kill every tmux session starting with 'debate-'.")
    sp.set_defaults(func=cmd_kill_all)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
