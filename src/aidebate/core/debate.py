"""End-to-end debate runner: opening -> cross-exam -> rebuttal -> verdict -> roast."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .adapter import load_adapter
from .crossexam import install_chat_helper, run_crossexam
from .events import EventLog
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

ROASTMASTER_ROLE = "roastmaster"
ROASTMASTER_AGENT = "claude"

ROASTMASTER_CLAUDE_MD = """\
# Roastmaster

You are the roastmaster for an AI debate that just wrapped up. Your only
job is to read the full transcript and verdict, then eviscerate every
participant — debaters, moderator, and yourself if warranted.

## Tone

- Foul-mouthed. Swear freely and specifically. Expletives are required,
  not garnish. "Fuck", "shit", "bastard", "asshole", "bitch" are all on
  the table; so is anything spicier that actually lands.
- Witty. Nothing sacred. No persona, quirk, or pretension off-limits.
- Punchy. Short paragraphs. Observations that cut.
- Honest. If someone said something genuinely smart, you may acknowledge
  it — then mock them for it anyway.

## What to target

For each participant, find:
- Pretentious vocabulary, marketing-speak, benchmark-dropping, hedge-words.
- Dodged questions, self-contradictions, misreads of an opponent.
- Recurring tics (always cites X, always retreats to Y, always opens with Z).
- The gap between what they claimed and what they actually delivered.
- Anything unintentionally revealing about their training or vendor.

## Structure

One section per participant with an `## <role> (<agent>)` header. End with
a short bonus paragraph roasting the whole cast together.

## What NOT to do

- Do not dial it down. No "constructive feedback." No "here's what went well."
- Do not moralize about the topic itself — roast the *debaters*, not their
  positions.
- Do not add warnings, disclaimers, content notes, or a polite closer.
- Do not summarize the debate. The roast IS the output.

If at any point you feel the urge to be gentle, reread the transcript
until the urge passes.
"""


@dataclass
class Side:
    role: str
    stance: str
    agent: str = "claude"  # adapter name


def _chat_blurb(chat_path: Path, role: str) -> str:
    return (
        "You share a group chat with the other debaters and the moderator at:\n"
        f"  {chat_path}\n\n"
        "To post a message, use the `chat-say` helper in your working "
        "directory — it handles timestamps and JSON formatting for you:\n"
        '  `./chat-say "Your message here"`                     (broadcast)\n'
        '  `./chat-say --to <role> "Directed message"`           (targeted)\n'
        '  `./chat-say --to <roleA>,<roleB> "For two roles"`     (multi)\n\n'
        f"Your role is: {role}. Read the chat tail (e.g. "
        f"`tail -n 40 {chat_path}`) before finalizing your answer, and "
        "address anything aimed at you. Keep chat messages short (≤2 "
        "sentences). Long-form reasoning goes in your answer file."
    )


def _format_chat_transcript(chat_path: Path, limit: int | None = None) -> str:
    if not chat_path.exists():
        return "(no chat)"
    lines = []
    raw = chat_path.read_text().splitlines()
    if limit is not None:
        raw = raw[-limit:]
    for line in raw:
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except Exception:
            continue
        to = ",".join(m.get("to") or []) or "*"
        lines.append(f"[{m.get('ts', '?')}] {m.get('from', '?')} -> {to}: {m.get('text', '')}")
    return "\n".join(lines) if lines else "(no chat)"


def _opening_prompt(topic: str, side: Side, chat_path: Path, all_roles: list[str]) -> str:
    others = [r for r in all_roles if r != side.role]
    return (
        f"# Debate — Opening statement\n\n"
        f"## Topic\n{topic}\n\n"
        f"## Your role: {side.role}\n"
        f"## Your stance\n{side.stance}\n\n"
        f"## Other debaters\n" + "\n".join(f"- {r}" for r in others) + "\n\n## Your task\n"
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
    chat_transcript: str,
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
        + "\n\n## Cross-examination transcript\n\n"
        f"This is the group chat from the cross-examination phase.\n\n"
        f"```\n{chat_transcript}\n```\n\n"
        "## Your task\n"
        "Write your rebuttal **in character with your stance above**. For each "
        "opponent, engage their strongest claim directly: if your stance is "
        "adversarial, exploit the most damaging flaw (factual error, logical "
        "gap, weak evidence, unacknowledged counter-example); if collaborative, "
        "sharpen the claim, concede what's sound, and push on what's still "
        "genuinely contested. Either way, **reference specific moments from "
        "the cross-examination** — a dodged question, a concession, a "
        "contradiction between their opening and what they said in chat. End "
        "with a 1–2 sentence defense of any point of yours your opponents are "
        "likely to attack. Keep it to ~300 words.\n\n"
        f"## Group chat\n{_chat_blurb(chat_path, side.role)}"
    )


def _verdict_prompt(
    topic: str,
    sides: list[Side],
    openings: dict[str, str],
    rebuttals: dict[str, str],
    chat_path: Path,
    chat_transcript: str,
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
            f"- `{d['role']}` ({d.get('agent', '?')}) — dropped during {d['phase']}: {d['error']}"
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
        + "\n\n## Cross-examination transcript\n\n"
        f"```\n{chat_transcript}\n```\n\n"
        f"(Full file on disk: {chat_path})\n\n"
        "## Your task\n"
        "You are the moderator. Render a reasoned verdict:\n\n"
        "1. **Winner** — which stance had the stronger case overall, and why "
        "(1 paragraph).\n"
        "2. **Scorecard** — for each debater: one sentence on their strongest "
        "contribution and one on their biggest weakness.\n"
        "3. **Open questions** — 2–3 questions this debate did not resolve "
        "that a decision-maker would still need to answer.\n\n"
        "Be concrete and willing to pick a winner. Avoid mushy "
        '"both sides have merit" conclusions unless the debate genuinely '
        "produced a tie."
    )


def _roast_prompt(
    topic: str,
    sides: list[Side],
    openings: dict[str, str],
    rebuttals: dict[str, str],
    verdict_text: str,
    chat_transcript: str,
    moderator_agent: str,
    dropouts: list[dict] | None = None,
) -> str:
    participants = "\n".join(f"- `{s.role}` ({s.agent}) — stance: {s.stance}" for s in sides)
    participants += f"\n- `moderator` ({moderator_agent})"
    opening_blocks = (
        "\n\n".join(f"### `{r}` opening\n\n{t}" for r, t in openings.items()) or "(none delivered)"
    )
    rebuttal_blocks = (
        "\n\n".join(f"### `{r}` rebuttal\n\n{t}" for r, t in rebuttals.items())
        or "(none delivered)"
    )
    dropout_note = ""
    if dropouts:
        lines = [
            f"- `{d['role']}` ({d.get('agent', '?')}) — dropped during {d['phase']}: {d['error']}"
            for d in dropouts
        ]
        dropout_note = (
            "\n\n## Participants who dropped out (fair game to roast for it)\n" + "\n".join(lines)
        )
    return (
        "# Post-debate roast\n\n"
        f"## Topic\n{topic}\n\n"
        f"## Participants\n{participants}\n"
        + dropout_note
        + "\n\n## Opening statements\n\n"
        + opening_blocks
        + "\n\n## Rebuttals\n\n"
        + rebuttal_blocks
        + "\n\n## Cross-examination transcript\n\n"
        f"```\n{chat_transcript}\n```\n\n"
        "## Moderator's verdict\n\n" + verdict_text + "\n\n## Your task\n"
        "Follow the tone and structure rules in your CLAUDE.md exactly. "
        "One `## <role> (<agent>)` section per participant (including "
        "`moderator`), then a short bonus paragraph for the whole cast. "
        "No dialing it down. No hedging. No closing summary."
    )


def run_debate(
    topic: str,
    sides: list[Side],
    moderator_agent: str = "claude",
    canary_timeout: float = 180.0,
    turn_timeout: float = 900.0,
    crossexam_wallclock: float = 300.0,
    crossexam_silence: float = 180.0,
    roast: bool = True,
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
        "sides": [{"role": s.role, "agent": s.agent, "stance": s.stance} for s in sides],
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "status": "running",
        "verdict_path": None,
        "completed_at": None,
    }
    manifest_path = session.root / "session.json"
    manifest["dropouts"] = []  # list[{role, agent, phase, error}]
    manifest_path.write_text(json.dumps(manifest, indent=2))

    events = EventLog(session.root / "events.jsonl")
    events.emit(
        "debate_started",
        topic=topic,
        moderator_agent=moderator_agent,
        sides=[{"role": s.role, "agent": s.agent, "stance": s.stance} for s in sides],
        roast_enabled=bool(roast),
    )

    def _record_dropout(role: str, phase: str, err: Exception, agent: str | None = None) -> None:
        agent_name = agent or next((s.agent for s in sides if s.role == role), "?")
        entry = {
            "role": role,
            "agent": agent_name,
            "phase": phase,
            "error": str(err),
            "at": datetime.now().isoformat(timespec="seconds"),
        }
        manifest["dropouts"].append(entry)
        manifest_path.write_text(json.dumps(manifest, indent=2))
        events.emit("dropout", role=role, agent=agent_name, phase=phase, error=str(err))
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

    roastmaster = None
    if roast:
        # Drop the roastmaster's CLAUDE.md into its cwd *before* launching
        # the claude CLI so it's picked up automatically at startup.
        roast_cwd = session.root / "agents" / ROASTMASTER_ROLE
        roast_cwd.mkdir(parents=True, exist_ok=True)
        (roast_cwd / "CLAUDE.md").write_text(ROASTMASTER_CLAUDE_MD)
        roastmaster = spawn_agent_pane(session, ROASTMASTER_ROLE, load_adapter(ROASTMASTER_AGENT))
    apply_moderator_layout(session, moderator_width_pct=33)

    if on_session_ready is not None:
        on_session_ready(session)

    # -----------------------------------------------------------------
    # Canary handshakes (parallel).
    # Moderator failure is fatal. Debater failures are logged; we carry
    # on with whoever survived, as long as at least one debater did.
    # -----------------------------------------------------------------
    print(f"[debate] canary handshakes (timeout {canary_timeout}s)...")
    events.emit("canary_started")

    canary_errors: dict[str, Exception] = {}

    def _canary(role: str, ap: AgentPane) -> None:
        try:
            canary_handshake(ap, timeout=canary_timeout)
        except Exception as e:
            canary_errors[role] = e

    ths = []
    canary_targets = {**panes, "moderator": moderator}
    if roastmaster is not None:
        canary_targets[ROASTMASTER_ROLE] = roastmaster
    for role, ap in canary_targets.items():
        th = threading.Thread(target=_canary, args=(role, ap), daemon=True)
        th.start()
        ths.append(th)
    for th in ths:
        th.join()

    if "moderator" in canary_errors:
        err = canary_errors["moderator"]
        print(f"[debate] MODERATOR CANARY FAILED: {err}")
        raise RuntimeError(f"moderator canary handshake failed: {err}")

    roastmaster_alive = roastmaster is not None and ROASTMASTER_ROLE not in canary_errors
    if roastmaster is not None and not roastmaster_alive:
        _record_dropout(
            ROASTMASTER_ROLE,
            "canary",
            canary_errors[ROASTMASTER_ROLE],
            agent=ROASTMASTER_AGENT,
        )

    for role, err in canary_errors.items():
        if role in ("moderator", ROASTMASTER_ROLE):
            continue
        _record_dropout(role, "canary", err)
        panes.pop(role, None)
    active_sides = [s for s in sides if s.role in panes]
    if not active_sides:
        raise RuntimeError("no debaters survived the canary handshake — cannot proceed")
    if canary_errors:
        survivors = ", ".join(s.role for s in active_sides)
        print(f"[debate] carrying on with {len(active_sides)} debater(s): {survivors}")
    else:
        print("[debate] all canaries OK")

    # Emit participant_ready for everyone who survived.
    for role, ap in canary_targets.items():
        if role in canary_errors:
            continue
        events.emit("participant_ready", role=role, agent=ap.adapter.name)

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
        events.emit("phase_started", phase=phase_name)
        tasks = [Task(panes[s.role], build_prompt(s)) for s in active_sides]
        results = run_parallel(tasks, phase_dir, timeout=turn_timeout)
        answers: dict[str, str] = {}
        for r, res in results.items():
            if res.error:
                _record_dropout(r, phase_name, res.error)
                panes.pop(r, None)
            elif res.answer is not None:
                answers[r] = res.answer
                events.emit("participant_completed_phase", role=r, phase=phase_name)
        # Update active_sides in place.
        active_sides[:] = [s for s in active_sides if s.role in panes]
        if not active_sides:
            raise RuntimeError(f"no debaters survived phase '{phase_name}' — cannot proceed")
        events.emit("phase_completed", phase=phase_name)
        return answers

    # Install the `chat-say` helper in every surviving pane's cwd so agents
    # can post to the group chat with a single shell call from any phase.
    install_chat_helper(chat_path, moderator.cwd, "moderator")
    for s in active_sides:
        install_chat_helper(chat_path, panes[s.role].cwd, s.role)

    # Phase 1 — Opening (parallel)
    print(f"[debate] phase 1: opening (parallel, {len(active_sides)} debater(s))")
    openings = _run_phase(
        "opening",
        session.root / "phase-1-opening",
        lambda s: _opening_prompt(topic, s, chat_path, [x.role for x in active_sides]),
    )

    # Phase 2 — Cross-examination (event-driven group chat)
    # Needs at least two debaters to meaningfully cross-examine. With only
    # one left, skip — there's nobody to question.
    if len(active_sides) >= 2:
        print(
            f"[debate] phase 2: cross-examination "
            f"(wallclock {crossexam_wallclock:.0f}s, silence {crossexam_silence:.0f}s, "
            f"{len(active_sides)} debater(s))"
        )
        events.emit("phase_started", phase="crossexam")
        run_crossexam(
            session_root=session.root,
            chat_path=chat_path,
            moderator=moderator,
            debaters={s.role: panes[s.role] for s in active_sides},
            stances={s.role: s.stance for s in active_sides},
            topic=topic,
            openings=openings,
            wallclock=crossexam_wallclock,
            silence_timeout=crossexam_silence,
            turn_timeout=min(turn_timeout, 300.0),
            event_log=events,
        )
        events.emit("phase_completed", phase="crossexam")
    else:
        print("[debate] phase 2: skipped (only one debater left — nothing to cross-examine)")
        events.emit("phase_skipped", phase="crossexam", reason="fewer than two debaters left")
    chat_transcript = _format_chat_transcript(chat_path)

    # Phase 3 — Rebuttal (parallel)
    # Only useful with 2+ surviving debaters; with 1 left, skip — there's
    # nothing to rebut.
    if len(active_sides) >= 2:
        print(f"[debate] phase 3: rebuttal (parallel, {len(active_sides)} debater(s))")
        rebuttals = _run_phase(
            "rebuttal",
            session.root / "phase-3-rebuttal",
            lambda s: _rebuttal_prompt(topic, s, openings, chat_path, chat_transcript),
        )
    else:
        print("[debate] phase 3: skipped (only one debater left — nothing to rebut)")
        events.emit("phase_skipped", phase="rebuttal", reason="fewer than two debaters left")
        rebuttals = {}

    # Phase 4 — Verdict (moderator)
    print("[debate] phase 4: verdict (moderator)")
    events.emit("phase_started", phase="verdict")
    phase4_dir = session.root / "phase-4-verdict"
    # Re-read transcript in case new chat arrived during rebuttal.
    chat_transcript = _format_chat_transcript(chat_path)
    verdict_text = run_turn(
        moderator,
        phase4_dir,
        _verdict_prompt(
            topic,
            active_sides,
            openings,
            rebuttals,
            chat_path,
            chat_transcript,
            dropouts=manifest["dropouts"],
        ),
        timeout=turn_timeout,
    )
    verdict_path = session.root / "verdict.md"
    verdict_path.write_text(verdict_text)
    events.emit("verdict_ready")
    events.emit("phase_completed", phase="verdict")
    print(f"[debate] verdict written to {verdict_path}")

    # Phase 5 — Roast (roastmaster). Bundled in unless caller opted out;
    # failure is non-fatal.
    manifest["roast_path"] = None
    manifest["roast_enabled"] = bool(roast)
    if not roast:
        print("[debate] phase 5: skipped (roast disabled by caller)")
        events.emit("phase_skipped", phase="roast", reason="disabled by caller")
    elif roastmaster_alive and roastmaster is not None:
        print("[debate] phase 5: roast (roastmaster)")
        events.emit("phase_started", phase="roast")
        phase5_dir = session.root / "phase-5-roast"
        chat_transcript = _format_chat_transcript(chat_path)
        try:
            roast_text = run_turn(
                roastmaster,
                phase5_dir,
                _roast_prompt(
                    topic,
                    active_sides,
                    openings,
                    rebuttals,
                    verdict_text,
                    chat_transcript,
                    moderator_agent,
                    dropouts=manifest["dropouts"],
                ),
                timeout=turn_timeout,
            )
            roast_path = session.root / "roast.md"
            roast_path.write_text(roast_text)
            manifest["roast_path"] = str(roast_path.relative_to(session.root))
            events.emit("roast_ready")
            events.emit("phase_completed", phase="roast")
            print(f"[debate] roast written to {roast_path}")
        except Exception as e:
            _record_dropout(ROASTMASTER_ROLE, "roast", e, agent=ROASTMASTER_AGENT)
            print(f"[debate] roast skipped: {e}")
    else:
        print("[debate] phase 5: skipped (roastmaster did not survive canary)")
        events.emit(
            "phase_skipped",
            phase="roast",
            reason="roastmaster did not survive canary",
        )

    # Finalize manifest.
    manifest["status"] = "done"
    manifest["verdict_path"] = str(verdict_path.relative_to(session.root))
    manifest["completed_at"] = datetime.now().isoformat(timespec="seconds")
    manifest_path.write_text(json.dumps(manifest, indent=2))
    events.emit("debate_completed", status="done")

    return session
