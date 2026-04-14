"""End-to-end debate runner: opening -> rebuttal -> verdict."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .adapter import load_adapter
from .pane import AgentPane
from .phases import Task, run_parallel
from .session import (
    DebateSession,
    apply_moderator_layout,
    create_session,
    enable_pane_titles,
    spawn_agent_pane,
)
from .turn import canary_handshake, run_turn


@dataclass
class Side:
    role: str
    stance: str
    agent: str = "claude"  # adapter name


def _chat_blurb(chat_path: Path, role: str) -> str:
    return (
        "You share a group chat with the other debaters and the moderator at:\n"
        f"  {chat_path}\n\n"
        "It is a JSONL file, one message per line. Format:\n"
        '  {"ts":"<ISO8601>","from":"<role>","to":["<role>"|"*"],"text":"..."}\n\n'
        f"Your role is: {role}. Before finalizing your answer, read the chat "
        "tail and address anything aimed at you (`to` contains your role or "
        '"*"). You may append short messages yourself — use `echo` with `>>`:\n'
        f'  echo \'{{"ts":"<iso>","from":"{role}","to":["*"],"text":"..."}}\' >> {chat_path}\n'
        "Keep chat messages short. Long-form reasoning goes in your answer file."
    )


def _opening_prompt(topic: str, side: Side, chat_path: Path, all_roles: list[str]) -> str:
    others = [r for r in all_roles if r != side.role]
    return (
        f"# Debate — Opening statement\n\n"
        f"## Topic\n{topic}\n\n"
        f"## Your role: {side.role}\n"
        f"## Your stance\n{side.stance}\n\n"
        f"## Other debaters\n"
        + "\n".join(f"- {r}" for r in others)
        + "\n\n## Your task\n"
        "Write a concise (~250 word) opening statement defending your stance. "
        "Give your thesis, your 3–5 strongest arguments with concrete evidence, "
        "and briefly acknowledge the strongest objection you expect.\n\n"
        f"## Group chat\n{_chat_blurb(chat_path, side.role)}"
    )


def _rebuttal_prompt(
    topic: str,
    side: Side,
    openings: dict[str, str],
    chat_path: Path,
) -> str:
    opp_blocks = []
    for role, text in openings.items():
        if role == side.role:
            continue
        opp_blocks.append(f"### Opening from `{role}`\n\n{text}")
    return (
        f"# Debate — Rebuttal\n\n"
        f"## Topic\n{topic}\n\n"
        f"## Your role: {side.role}\n"
        f"## Your stance\n{side.stance}\n\n"
        f"## Opponents' opening statements\n\n"
        + "\n\n".join(opp_blocks)
        + "\n\n## Your task\n"
        "Act as a hostile reviewer. For each opponent, identify the single "
        "most damaging flaw in their argument (factual error, logical gap, "
        "weak evidence, or unacknowledged counter-example) and exploit it. "
        "End with a 1–2 sentence defense of any point of yours your opponents "
        "are likely to attack. Keep it to ~300 words total.\n\n"
        f"## Group chat\n{_chat_blurb(chat_path, side.role)}"
    )


def _verdict_prompt(
    topic: str,
    sides: list[Side],
    openings: dict[str, str],
    rebuttals: dict[str, str],
    chat_path: Path,
    dropouts: list[dict] | None = None,
) -> str:
    side_blocks = []
    for s in sides:
        side_blocks.append(
            f"### `{s.role}` — stance: {s.stance}\n\n"
            f"**Opening:**\n\n{openings.get(s.role, '(no opening)')}\n\n"
            f"**Rebuttal:**\n\n{rebuttals.get(s.role, '(no rebuttal)')}"
        )
    dropout_note = ""
    if dropouts:
        lines = [
            f"- `{d['role']}` ({d.get('agent','?')}) — dropped during {d['phase']}: {d['error']}"
            for d in dropouts
        ]
        dropout_note = (
            "\n\n## Participants who dropped out\n"
            "These debaters failed to deliver (agent crashed, auth expired, "
            "CLI misbehaved, etc.). Judge only the debaters who did submit; "
            "do NOT penalize them for the dropouts or invent positions for "
            "the absent ones.\n\n" + "\n".join(lines)
        )
    return (
        "# Debate — Moderator verdict\n\n"
        f"## Topic\n{topic}\n\n"
        "## Debater submissions\n\n"
        + "\n\n---\n\n".join(side_blocks)
        + dropout_note
        + f"\n\n## Group chat transcript\nYou may also consult: {chat_path}\n\n"
        "## Your task\n"
        "You are the moderator. Render a reasoned verdict:\n\n"
        "1. **Winner** — which stance had the stronger case overall, and why "
        "(1 paragraph).\n"
        "2. **Scorecard** — for each debater: one sentence on their strongest "
        "contribution and one on their biggest weakness.\n"
        "3. **Open questions** — 2–3 questions this debate did not resolve "
        "that a decision-maker would still need to answer.\n\n"
        "Be concrete and willing to pick a winner. Avoid mushy "
        "\"both sides have merit\" conclusions unless the debate genuinely "
        "produced a tie."
    )


def run_debate(
    topic: str,
    sides: list[Side],
    moderator_agent: str = "claude",
    canary_timeout: float = 180.0,
    turn_timeout: float = 900.0,
    on_session_ready=None,  # callback(session) -> None, called after panes spawn
) -> DebateSession:
    session = create_session()
    enable_pane_titles(session)
    print(f"[debate] session_id={session.session_id}")
    print(f"[debate] topic: {topic}")

    # Write an initial manifest so the session is discoverable even if the
    # run aborts or the process crashes.
    manifest = {
        "session_id": session.session_id,
        "topic": topic,
        "moderator_agent": moderator_agent,
        "sides": [
            {"role": s.role, "agent": s.agent, "stance": s.stance} for s in sides
        ],
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "status": "running",
        "verdict_path": None,
        "completed_at": None,
    }
    manifest_path = session.root / "session.json"
    manifest["dropouts"] = []  # list[{role, agent, phase, error}]
    manifest_path.write_text(json.dumps(manifest, indent=2))

    def _record_dropout(role: str, phase: str, err: Exception) -> None:
        agent_name = next(
            (s.agent for s in sides if s.role == role), "?"
        )
        entry = {
            "role": role,
            "agent": agent_name,
            "phase": phase,
            "error": str(err),
            "at": datetime.now().isoformat(timespec="seconds"),
        }
        manifest["dropouts"].append(entry)
        manifest_path.write_text(json.dumps(manifest, indent=2))
        print(f"[debate] DROPOUT: {role}@{agent_name} failed during {phase}: {err}")

    # Spawn moderator first so it becomes the main (leftmost) pane; then
    # debaters split off to the right. Each participant may use a different
    # agent engine.
    assignment = ", ".join(f"{s.role}@{s.agent}" for s in sides)
    print(f"[debate] spawning panes: moderator@{moderator_agent} + {assignment}")
    moderator = spawn_agent_pane(session, "moderator", load_adapter(moderator_agent))
    panes: dict[str, AgentPane] = {}
    for side in sides:
        panes[side.role] = spawn_agent_pane(session, side.role, load_adapter(side.agent))
    apply_moderator_layout(session, moderator_width_pct=33)

    if on_session_ready is not None:
        on_session_ready(session)

    # -----------------------------------------------------------------
    # Canary handshakes (parallel).
    # Moderator failure is fatal. Debater failures are logged; we carry
    # on with whoever survived, as long as at least one debater did.
    # -----------------------------------------------------------------
    print(f"[debate] canary handshakes (timeout {canary_timeout}s)...")
    import threading
    canary_errors: dict[str, Exception] = {}

    def _canary(role: str, ap: AgentPane) -> None:
        try:
            canary_handshake(ap, timeout=canary_timeout)
        except Exception as e:
            canary_errors[role] = e

    ths = []
    for role, ap in {**panes, "moderator": moderator}.items():
        th = threading.Thread(target=_canary, args=(role, ap), daemon=True)
        th.start()
        ths.append(th)
    for th in ths:
        th.join()

    if "moderator" in canary_errors:
        err = canary_errors["moderator"]
        print(f"[debate] MODERATOR CANARY FAILED: {err}")
        raise RuntimeError(f"moderator canary handshake failed: {err}")

    for role, err in canary_errors.items():
        if role == "moderator":
            continue
        _record_dropout(role, "canary", err)
        panes.pop(role, None)
    active_sides = [s for s in sides if s.role in panes]
    if not active_sides:
        raise RuntimeError(
            "no debaters survived the canary handshake — cannot proceed"
        )
    if canary_errors:
        survivors = ", ".join(s.role for s in active_sides)
        print(f"[debate] carrying on with {len(active_sides)} debater(s): {survivors}")
    else:
        print("[debate] all canaries OK")

    chat_path = session.chat_path

    # -----------------------------------------------------------------
    # Helper: run a parallel phase, drop anyone who fails, and require
    # at least one survivor to continue.
    # -----------------------------------------------------------------
    def _run_phase(
        phase_name: str,
        phase_dir: Path,
        build_prompt,  # (Side) -> str
    ) -> dict[str, str]:
        tasks = [Task(panes[s.role], build_prompt(s)) for s in active_sides]
        results = run_parallel(tasks, phase_dir, timeout=turn_timeout)
        answers: dict[str, str] = {}
        for r, res in results.items():
            if res.error:
                _record_dropout(r, phase_name, res.error)
                panes.pop(r, None)
            elif res.answer is not None:
                answers[r] = res.answer
        # Update active_sides in place.
        active_sides[:] = [s for s in active_sides if s.role in panes]
        if not active_sides:
            raise RuntimeError(
                f"no debaters survived phase '{phase_name}' — cannot proceed"
            )
        return answers

    # Phase 1 — Opening (parallel)
    print(f"[debate] phase 1: opening (parallel, {len(active_sides)} debater(s))")
    openings = _run_phase(
        "opening",
        session.root / "phase-1-opening",
        lambda s: _opening_prompt(
            topic, s, chat_path, [x.role for x in active_sides]
        ),
    )

    # Phase 2 — Rebuttal (parallel)
    # Only useful with 2+ surviving debaters; with 1 left, skip — there's
    # nothing to rebut.
    if len(active_sides) >= 2:
        print(f"[debate] phase 2: rebuttal (parallel, {len(active_sides)} debater(s))")
        rebuttals = _run_phase(
            "rebuttal",
            session.root / "phase-2-rebuttal",
            lambda s: _rebuttal_prompt(topic, s, openings, chat_path),
        )
    else:
        print("[debate] phase 2: skipped (only one debater left — nothing to rebut)")
        rebuttals = {}

    # Phase 3 — Verdict (moderator)
    print("[debate] phase 3: verdict (moderator)")
    phase3_dir = session.root / "phase-3-verdict"
    verdict_text = run_turn(
        moderator,
        phase3_dir,
        _verdict_prompt(
            topic, active_sides, openings, rebuttals, chat_path,
            dropouts=manifest["dropouts"],
        ),
        timeout=turn_timeout,
    )
    verdict_path = session.root / "verdict.md"
    verdict_path.write_text(verdict_text)
    print(f"[debate] verdict written to {verdict_path}")

    # Finalize manifest.
    manifest["status"] = "done"
    manifest["verdict_path"] = str(verdict_path.relative_to(session.root))
    manifest["completed_at"] = datetime.now().isoformat(timespec="seconds")
    manifest_path.write_text(json.dumps(manifest, indent=2))

    return session
