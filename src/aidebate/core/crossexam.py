"""Cross-examination phase: event-driven group chat between debaters.

Model:

- Every participant (debaters + moderator) has its own worker thread with a
  message queue. Only one turn per role runs at a time; incoming nudges that
  arrive while the role is busy are batched into the next turn.

- A ``chat-say`` helper script is installed in each agent's cwd so agents
  don't have to hand-format JSONL. The helper appends a well-formed message
  to ``chat.jsonl`` atomically (single short write in append mode).

- A chat watcher thread tails ``chat.jsonl``, decodes new messages, and
  enqueues a nudge for each addressee (``to`` contains their role, or
  ``"*"`` broadcasts to everyone except the sender).

- A silence watcher thread nudges the *moderator* when chat has been quiet
  for a while, so the moderator can steer or provoke.

- The phase ends when either (a) wallclock expires, or (b) no new chat
  message has arrived within ``silence_timeout``. In-flight turns are given
  a bounded grace period to finish before the phase returns.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .pane import AgentPane
from .turn import run_turn


# ---------------------------------------------------------------------------
# chat-say helper: installed into each agent's cwd so the LLM can post a
# message with a single shell call instead of hand-rolling JSON.
# ---------------------------------------------------------------------------


def _chat_say_script(chat_path: Path, role: str) -> str:
    # Using repr() to embed Python string literals means we don't have to
    # worry about shell quoting of paths or roles that contain odd chars.
    return (
        "#!/usr/bin/env python3\n"
        '"""chat-say: post a message to the debate group chat."""\n'
        "import argparse, json\n"
        "from datetime import datetime, timezone\n"
        f"CHAT = {str(chat_path)!r}\n"
        f"ROLE = {str(role)!r}\n"
        "p = argparse.ArgumentParser(description='Post to the debate chat.')\n"
        "p.add_argument('--to', default='*',\n"
        "               help='Comma-separated roles, or \"*\" for broadcast.')\n"
        "p.add_argument('text', help='The message body (quote it).')\n"
        "a = p.parse_args()\n"
        "to = ['*'] if a.to.strip() in ('*', '') else [\n"
        "    r.strip() for r in a.to.split(',') if r.strip()]\n"
        "msg = {'ts': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),\n"
        "       'from': ROLE, 'to': to, 'text': a.text}\n"
        "with open(CHAT, 'a') as f:\n"
        "    f.write(json.dumps(msg, ensure_ascii=False) + '\\n')\n"
        "print('posted')\n"
    )


def install_chat_helper(chat_path: Path, agent_cwd: Path, role: str) -> Path:
    """Write an executable ``chat-say`` script into ``agent_cwd``."""
    helper = agent_cwd / "chat-say"
    helper.write_text(_chat_say_script(chat_path, role))
    helper.chmod(0o755)
    return helper


# ---------------------------------------------------------------------------
# Chat message parsing
# ---------------------------------------------------------------------------


def _read_chat(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _is_addressee(msg: dict, role: str) -> bool:
    to = msg.get("to") or []
    return "*" in to or role in to


# ---------------------------------------------------------------------------
# Prompt fragments
# ---------------------------------------------------------------------------


def _chat_tail_fmt(msgs: list[dict], limit: int = 30) -> str:
    tail = msgs[-limit:]
    if not tail:
        return "(chat is empty so far)"
    lines = []
    for m in tail:
        to = ",".join(m.get("to") or []) or "*"
        lines.append(f"[{m.get('ts','?')}] {m.get('from','?')} -> {to}: {m.get('text','')}")
    return "\n".join(lines)


def _seed_prompt_debater(
    role: str,
    stance: str,
    topic: str,
    openings: dict[str, str],
    other_roles: list[str],
    chat_path: Path,
) -> str:
    opp_blocks = "\n\n".join(
        f"### {r}\n{openings.get(r, '(no opening)')}" for r in other_roles if r != role
    )
    return (
        f"# Cross-examination — opening volley\n\n"
        f"## Topic\n{topic}\n\n"
        f"## Your role: {role}\n## Your stance\n{stance}\n\n"
        f"## Opponents' openings\n{opp_blocks}\n\n"
        f"## Your task\n"
        f"Post **2 pointed, specific questions** to your opponents that attack the "
        f"weakest concrete claim in their opening. Target each question at a "
        f"specific role (use `--to`). Keep each question to one or two sentences; "
        f"make them sharp, not rhetorical.\n\n"
        f"Post via the helper in this directory:\n"
        f"  `./chat-say --to <role> \"Your question here?\"`\n\n"
        f"After posting, write `(posted)` to your answer file and touch .done."
    )


def _seed_prompt_moderator(
    topic: str,
    sides_desc: str,
    openings: dict[str, str],
    chat_path: Path,
) -> str:
    opening_blocks = "\n\n".join(
        f"### {r}\n{t}" for r, t in openings.items()
    )
    return (
        f"# Cross-examination — moderator watch\n\n"
        f"## Topic\n{topic}\n\n"
        f"## Debaters\n{sides_desc}\n\n"
        f"## Openings\n{opening_blocks}\n\n"
        f"## Your task\n"
        f"You are the moderator during an open cross-examination. Watch the "
        f"group chat at `{chat_path}`. Stay out of the way when debaters are "
        f"engaging each other usefully. Step in ONLY when:\n"
        f"  - a debater dodges a direct question,\n"
        f"  - the exchange goes in circles, or\n"
        f"  - a critical assumption is going unchallenged.\n\n"
        f"For this first turn: read the openings above and post ONE sharp "
        f"framing question to the whole group to kick off the exchange.\n\n"
        f"Post via:\n"
        f"  `./chat-say --to <role>[,<role>] \"...\"`   (targeted)\n"
        f"  `./chat-say \"...\"`                         (broadcast)\n\n"
        f"After posting, write `(posted)` to your answer file and touch .done."
    )


def _nudge_prompt(
    role: str,
    stance: str | None,
    is_moderator: bool,
    batched: list[dict],
    recent_chat: list[dict],
    chat_path: Path,
) -> str:
    addressed_block = "\n".join(
        f"- from `{m.get('from','?')}` (to {','.join(m.get('to') or []) or '*'}): "
        f"{m.get('text','')}"
        for m in batched
    ) or "(none — general nudge)"
    role_header = (
        f"## Your role: {role} (moderator)"
        if is_moderator
        else f"## Your role: {role}\n## Your stance\n{stance}"
    )
    pass_rule = (
        "If you have nothing sharp to add right now, reply `(pass)` — "
        "write `(pass)` to your answer file and touch .done WITHOUT posting "
        "to chat. Silence is better than filler."
    )
    if is_moderator:
        action = (
            "If a debater dodged a question or the exchange is stalling, "
            "post a single short targeted question to get it moving. "
            "Otherwise " + pass_rule.lower()
        )
    else:
        action = (
            "Respond with ONE short message (≤2 sentences). Either answer "
            "the question directly, or, if it's a bad question, say why in "
            "one sentence and redirect. " + pass_rule
        )
    return (
        f"# Cross-examination — new activity\n\n"
        f"{role_header}\n\n"
        f"## Messages addressed to you since your last turn\n{addressed_block}\n\n"
        f"## Recent chat (last {len(recent_chat)})\n{_chat_tail_fmt(recent_chat)}\n\n"
        f"## Your task\n{action}\n\n"
        f"To post: `./chat-say --to <role>[,<role>] \"...\"` (targeted) "
        f"or `./chat-say \"...\"` (broadcast). Then write `(posted)` to your "
        f"answer file and touch .done."
    )


def _stall_prompt_moderator(
    recent_chat: list[dict],
    chat_path: Path,
) -> str:
    return (
        f"# Cross-examination — chat has stalled\n\n"
        f"No new messages have arrived for a while. As moderator, post ONE "
        f"sharp provocation or targeted question to restart the exchange. "
        f"Pick the weakest unanswered point from recent chat, or push a "
        f"debater on a claim they're avoiding.\n\n"
        f"## Recent chat (last 30)\n{_chat_tail_fmt(recent_chat)}\n\n"
        f"## Your task\n"
        f"Post ONE message via `./chat-say` (targeted or broadcast). Or, "
        f"if the debate truly has nothing left to give, reply `(pass)` "
        f"— write `(pass)` to your answer file and touch .done without "
        f"posting.\n"
    )


# ---------------------------------------------------------------------------
# Cross-exam driver
# ---------------------------------------------------------------------------


_SEED_MARKER = "__SEED__"  # sentinel queue item that triggers a seed-style turn


@dataclass
class _RoleState:
    role: str
    agent: AgentPane
    q: queue.Queue = field(default_factory=queue.Queue)
    idle: threading.Event = field(default_factory=threading.Event)
    turn_n: int = 0


def run_crossexam(
    *,
    session_root: Path,
    chat_path: Path,
    moderator: AgentPane,
    debaters: dict[str, AgentPane],          # role -> pane
    stances: dict[str, str],                 # role -> stance
    topic: str,
    openings: dict[str, str],
    wallclock: float = 300.0,
    silence_timeout: float = 180.0,
    moderator_silence_nudge: float = 60.0,
    turn_timeout: float = 300.0,
) -> Path:
    """Run the cross-examination phase. Returns the phase directory."""
    phase_dir = session_root / "phase-2-crossexam"
    phase_dir.mkdir(exist_ok=True)

    # Install chat-say helper in each pane's cwd (idempotent).
    install_chat_helper(chat_path, moderator.cwd, "moderator")
    for role, ap in debaters.items():
        install_chat_helper(chat_path, ap.cwd, role)

    all_panes: dict[str, AgentPane] = {"moderator": moderator, **debaters}
    states: dict[str, _RoleState] = {
        role: _RoleState(role=role, agent=ap) for role, ap in all_panes.items()
    }
    for st in states.values():
        st.idle.set()

    stop = threading.Event()

    def _do_turn(role: str, prompt_body: str) -> None:
        st = states[role]
        st.idle.clear()
        try:
            st.turn_n += 1
            n = st.turn_n
            turn_dir = phase_dir / role / f"turn-{n:03d}"
            try:
                run_turn(st.agent, turn_dir, prompt_body, timeout=turn_timeout)
            except Exception as e:
                # Record but don't kill the phase — another role may still drive.
                err = turn_dir / "error.log"
                turn_dir.mkdir(parents=True, exist_ok=True)
                err.write_text(f"{type(e).__name__}: {e}\n")
                print(f"[crossexam] {role} turn-{n} failed: {e}")
        finally:
            st.idle.set()

    # --- Per-role workers ------------------------------------------------

    def _worker(role: str) -> None:
        st = states[role]
        is_mod = role == "moderator"
        other_roles = [r for r in debaters.keys() if r != role]
        while not stop.is_set():
            try:
                first = st.q.get(timeout=0.5)
            except queue.Empty:
                continue
            batch = [first]
            while True:
                try:
                    batch.append(st.q.get_nowait())
                except queue.Empty:
                    break

            # Seed?
            if _SEED_MARKER in batch:
                if is_mod:
                    sides_desc = "\n".join(
                        f"- {r}: {stances.get(r, '(no stance)')}"
                        for r in debaters.keys()
                    )
                    prompt = _seed_prompt_moderator(
                        topic, sides_desc, openings, chat_path
                    )
                else:
                    prompt = _seed_prompt_debater(
                        role,
                        stances.get(role, ""),
                        topic,
                        openings,
                        list(debaters.keys()),
                        chat_path,
                    )
                _do_turn(role, prompt)
                # If any non-seed items came with it, leave them; they'll be
                # reprocessed on the next drain. Messages older than the seed
                # turn aren't super interesting.
                continue

            # Regular nudge: filter to real chat messages; drop duplicates
            # by (ts, from, text).
            msgs = [m for m in batch if isinstance(m, dict)]
            # Include only those addressed to this role (or broadcast).
            addressed = [m for m in msgs if _is_addressee(m, role) and m.get("from") != role]
            if not addressed and not is_mod:
                continue
            recent = _read_chat(chat_path)
            if is_mod and not addressed:
                # Stall nudge for moderator.
                prompt = _stall_prompt_moderator(recent, chat_path)
            else:
                prompt = _nudge_prompt(
                    role,
                    stances.get(role),
                    is_mod,
                    addressed,
                    recent,
                    chat_path,
                )
            _do_turn(role, prompt)

    # --- Chat watcher: dispatches messages to role queues ---------------

    last_message_time = threading.Event()  # used as a signal, not a flag
    last_message_ts = [time.monotonic()]
    lock = threading.Lock()

    def _chat_watcher() -> None:
        seen = 0
        while not stop.is_set():
            msgs = _read_chat(chat_path)
            if len(msgs) > seen:
                for m in msgs[seen:]:
                    sender = m.get("from")
                    with lock:
                        last_message_ts[0] = time.monotonic()
                    for role in all_panes.keys():
                        if role == sender:
                            continue
                        if _is_addressee(m, role):
                            states[role].q.put(m)
                seen = len(msgs)
            time.sleep(0.75)

    # --- Silence watcher: prods the moderator if chat goes quiet --------

    def _silence_watcher() -> None:
        last_prod = 0.0
        while not stop.is_set():
            time.sleep(1.0)
            with lock:
                since = time.monotonic() - last_message_ts[0]
            # Only prod moderator if silence > moderator_silence_nudge,
            # and no more than once per moderator_silence_nudge window.
            if since >= moderator_silence_nudge and (time.monotonic() - last_prod) >= moderator_silence_nudge:
                # Use a synthetic non-addressed item so the worker falls into
                # the moderator stall branch.
                states["moderator"].q.put({"__stall__": True})
                last_prod = time.monotonic()

    # --- Start threads ---------------------------------------------------

    workers: list[threading.Thread] = []
    for role in all_panes.keys():
        th = threading.Thread(target=_worker, args=(role,), daemon=True, name=f"cx-{role}")
        th.start()
        workers.append(th)

    # Seed everyone in parallel.
    for role in all_panes.keys():
        states[role].q.put(_SEED_MARKER)

    cw = threading.Thread(target=_chat_watcher, daemon=True, name="cx-chat")
    sw = threading.Thread(target=_silence_watcher, daemon=True, name="cx-silence")
    cw.start()
    sw.start()

    # --- Termination loop -----------------------------------------------

    start = time.monotonic()
    while True:
        time.sleep(1.0)
        now = time.monotonic()
        if now - start >= wallclock:
            print(f"[crossexam] wallclock reached ({wallclock:.0f}s)")
            break
        with lock:
            since_msg = now - last_message_ts[0]
        if since_msg >= silence_timeout:
            print(f"[crossexam] silence timeout reached ({silence_timeout:.0f}s idle)")
            break

    stop.set()

    # Wait a bounded time for in-flight turns to drain.
    drain_deadline = time.time() + 30.0
    for role, st in states.items():
        remaining = max(0.0, drain_deadline - time.time())
        if not st.idle.wait(timeout=remaining):
            print(f"[crossexam] warning: {role} turn still in flight at phase end")

    return phase_dir
