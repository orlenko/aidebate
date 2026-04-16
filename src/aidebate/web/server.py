"""FastAPI web UI for the debate orchestrator.

One-process app:
  - POST /api/debates starts a debate in a worker thread.
  - A poller thread per session tails every tmux pane and every answer.md,
    and pushes events into an in-memory queue.
  - GET /api/debates/{id}/events streams those events over SSE.
  - A single HTML page (`static/index.html`) renders the form and the
    live view.
"""

from __future__ import annotations

import asyncio
import collections
import json
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from aidebate import __version__
from aidebate.core.adapter import ADAPTERS_DIR
from aidebate.core.debate import Side, run_debate
from aidebate.core.events import EventLog, read_events
from aidebate.core.session import DebateSession, sessions_root

HERE = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Per-subscriber event buffer
# ---------------------------------------------------------------------------


class EventBuffer:
    """Bounded FIFO buffer for one SSE subscriber, with priority-aware drop.

    When full, eviction picks the oldest *non-priority* event so that
    terminal events (verdict, roast, status, error, debate_completed)
    can never be silently dropped under pane-event pressure. If the
    buffer happens to be at capacity with all priority events — a
    degenerate case — the new event is still appended (temporary
    overflow), because losing a terminal event is worse than a one-tick
    size bump. Consumers drain via `get(timeout)` which mirrors
    `queue.Queue.get(block=True, timeout=...)` and raises `queue.Empty`
    when the timeout elapses.
    """

    PRIORITY_TYPES = frozenset({"verdict", "roast", "status", "error", "debate_completed"})

    def __init__(self, maxsize: int) -> None:
        self._maxsize = maxsize
        self._items: collections.deque[dict] = collections.deque()
        self._cond = threading.Condition()

    def put(self, item: dict) -> None:
        with self._cond:
            if len(self._items) >= self._maxsize:
                # Evict the oldest non-priority event. Walking the deque
                # is O(n) but n<=maxsize and only fires under overflow,
                # which is rare — acceptable for the correctness win.
                for i, existing in enumerate(self._items):
                    if existing.get("type") not in self.PRIORITY_TYPES:
                        del self._items[i]
                        break
                # else: no evictable event — fall through and overflow.
            self._items.append(item)
            self._cond.notify()

    def get(self, timeout: float | None = None) -> dict:
        with self._cond:
            if timeout is None:
                while not self._items:
                    self._cond.wait()
            else:
                end = time.monotonic() + timeout
                while not self._items:
                    remaining = end - time.monotonic()
                    if remaining <= 0:
                        raise queue.Empty
                    self._cond.wait(remaining)
            return self._items.popleft()


# ---------------------------------------------------------------------------
# In-memory session registry
# ---------------------------------------------------------------------------


@dataclass
class SessionState:
    session_id: str
    topic: str
    sides: list[dict]  # serializable copy of sides
    moderator_agent: str
    crossexam_wallclock: float = 300.0
    crossexam_silence: float = 180.0
    # Was the roast phase enabled for this debate? (boolean config flag).
    # Kept distinct from `roast` (the rendered roast content) after a
    # previous collision where both attributes shared a name and the later
    # one silently shadowed the earlier — any `if state.roast:` check was
    # lying about its meaning half the time.
    roast_enabled: bool = True
    debate_session: DebateSession | None = None
    thread: threading.Thread | None = None
    poller: threading.Thread | None = None
    status: str = "starting"  # starting, running, done, error
    error: str | None = None
    verdict: str | None = None
    # Content of roast.md after phase 5 completes, or None if not yet run.
    roast: str | None = None
    events: list[dict] = field(default_factory=list)
    # Narrative events (play-by-play) kept in a separate, untrimmed buffer
    # so reconnecting clients always get the full story even on long
    # debates where pane captures have pushed other events out of `events`.
    # Narrative events are low-volume (expected <100 per debate), so no
    # trim is needed.
    narrative_events: list[dict] = field(default_factory=list)
    subscribers: list[EventBuffer] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    # Monotonic event id counter. Previously this used `len(self.events)`,
    # which froze at EVENT_BUFFER_MAX once the buffer capped — every event
    # after that got the same id. Keeping a separate counter lets clients
    # dedupe or `since_id` replay correctly even across cap boundaries.
    _next_event_id: int = 0

    # Cap the in-memory event buffer so long-running servers don't
    # accumulate unbounded pane captures. New subscribers still get a
    # backlog, just a trimmed one. Does not apply to `narrative_events`.
    EVENT_BUFFER_MAX: int = 500

    def emit(self, event: dict) -> None:
        with self._lock:
            event = {"id": self._next_event_id, **event, "ts": time.time()}
            self._next_event_id += 1
            self.events.append(event)
            if event.get("type") == "narrative":
                self.narrative_events.append(event)
            if len(self.events) > self.EVENT_BUFFER_MAX:
                # Drop the oldest chunk in one shot to keep the common
                # path O(1) amortized.
                self.events = self.events[-self.EVENT_BUFFER_MAX :]
            subs = list(self.subscribers)
        # EventBuffer.put handles priority-aware eviction internally —
        # terminal events (verdict, roast, status=done, etc.) survive
        # even under sustained pane-event pressure.
        for q in subs:
            q.put(event)


SESSIONS: dict[str, SessionState] = {}
SESSIONS_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Poller: captures pane text + answer files; emits change events
# ---------------------------------------------------------------------------


def _capture_pane(pane) -> str:
    try:
        out = pane.cmd("capture-pane", "-p", "-S", "-200").stdout
        return "\n".join(out)
    except Exception:
        return ""


def _poll_session(state: SessionState) -> None:
    """Poll tmux panes, answer files, and events.jsonl; emit change events."""
    last_pane_text: dict[str, str] = {}
    last_answers: dict[str, str] = {}
    last_final: dict[str, str] = {}  # verdict.md / roast.md content last seen
    events_pos = 0  # byte offset into events.jsonl already forwarded
    # Wait for run_debate to spawn panes.
    waits = 0
    while state.debate_session is None and waits < 60:
        time.sleep(0.5)
        waits += 1
    if state.debate_session is None:
        return

    session = state.debate_session
    state.emit(
        {
            "type": "session_ready",
            "session_id": session.session_id,
            "tmux": f"debate-{session.session_id}",
        }
    )

    events_path = session.root / "events.jsonl"

    def _drain_events() -> int:
        nonlocal events_pos
        if not events_path.exists():
            return events_pos
        try:
            with events_path.open("r") as f:
                f.seek(events_pos)
                chunk = f.read()
                events_pos = f.tell()
        except OSError:
            return events_pos
        for line in chunk.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            state.emit({"type": "narrative", "event": ev})
        return events_pos

    while state.status in ("starting", "running"):
        _drain_events()
        # Pane captures.
        for role, ap in list(session.panes.items()):
            text = _capture_pane(ap.pane)
            if text and text != last_pane_text.get(role):
                last_pane_text[role] = text
                state.emit({"type": "pane", "role": role, "text": text})
        # Answer files (per-phase).
        for phase_dir in sorted(session.root.glob("phase-*")):
            for ans in phase_dir.glob("*.answer.md"):
                role = ans.name[: -len(".answer.md")]
                try:
                    content = ans.read_text()
                except OSError:
                    continue
                key = f"{phase_dir.name}:{role}"
                if content != last_answers.get(key):
                    last_answers[key] = content
                    state.emit(
                        {
                            "type": "answer",
                            "role": role,
                            "phase": phase_dir.name,
                            "content": content,
                        }
                    )
        # Verdict + roast — emit as soon as the file appears on disk so the
        # live UI doesn't have to wait for the WHOLE debate (including roast)
        # to finish before showing the verdict tab. Without this, the
        # `verdict` / `roast` events were only emitted from _run_debate_thread
        # after run_debate returned, which is after phase 5, not phase 4.
        for name in ("verdict", "roast"):
            f = session.root / f"{name}.md"
            if not f.exists():
                continue
            try:
                content = f.read_text()
            except OSError:
                continue
            if content != last_final.get(name):
                last_final[name] = content
                # Keep state in sync so page-load REST endpoints return the
                # same content the live subscribers already received.
                setattr(state, name, content)
                state.emit({"type": name, "content": content})
        time.sleep(1.0)

    # Final drain so the last few narrative events (verdict_ready,
    # roast_ready, debate_completed) always make it to live subscribers.
    _drain_events()

    # Final pass to capture anything written between the last poll and the end.
    if session is not None:
        for role, ap in list(session.panes.items()):
            text = _capture_pane(ap.pane)
            if text and text != last_pane_text.get(role):
                state.emit({"type": "pane", "role": role, "text": text})


# ---------------------------------------------------------------------------
# Worker: runs the debate
# ---------------------------------------------------------------------------


def _run_debate_thread(state: SessionState) -> None:
    try:
        sides = [Side(role=s["role"], stance=s["stance"], agent=s["agent"]) for s in state.sides]

        def _on_ready(session: DebateSession) -> None:
            state.debate_session = session
            state.status = "running"
            state.emit({"type": "status", "status": "running"})

        session = run_debate(
            topic=state.topic,
            sides=sides,
            moderator_agent=state.moderator_agent,
            crossexam_wallclock=state.crossexam_wallclock,
            crossexam_silence=state.crossexam_silence,
            roast=state.roast_enabled,
            on_session_ready=_on_ready,
        )
        state.status = "done"
        verdict_path = session.root / "verdict.md"
        if verdict_path.exists():
            state.verdict = verdict_path.read_text()
            state.emit({"type": "verdict", "content": state.verdict})
        roast_path = session.root / "roast.md"
        if roast_path.exists():
            state.roast = roast_path.read_text()
            state.emit({"type": "roast", "content": state.roast})
        state.emit({"type": "status", "status": "done"})
    except Exception as e:
        state.status = "error"
        state.error = str(e)
        # Append a debate_completed event so the archive view renders the
        # failure in the play-by-play just like a successful finish.
        if state.debate_session is not None:
            try:
                EventLog(state.debate_session.root / "events.jsonl").emit(
                    "debate_completed", status="error", error=str(e)
                )
            except OSError as log_err:
                # Don't let logging errors mask the real failure; note it.
                print(f"[server] failed to record debate_completed event: {log_err}")
        state.emit({"type": "error", "message": str(e)})
        state.emit({"type": "status", "status": "error"})
    finally:
        # Tear down the tmux session so panes + their child agent processes
        # don't leak. Wait briefly for the poller to finish its final drain
        # + pane captures first, so those race-free against a live tmux.
        if state.poller is not None and state.poller.is_alive():
            state.poller.join(timeout=5.0)
        if state.debate_session is not None:
            state.debate_session.kill()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


app = FastAPI(title="debate")
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(HERE / "static" / "index.html")


@app.get("/api/sessions")
def list_sessions() -> list[dict]:
    """List stored sessions on disk, newest first.

    Each entry is a lightweight summary read from `session.json` when
    available, falling back to the session id alone for legacy runs.
    """
    if not sessions_root().exists():
        return []
    # "Really running" = we have in-memory SessionState for it. A manifest
    # that still says running but whose SessionState is gone (server
    # restarted mid-run) gets downgraded to "stale" so the UI doesn't
    # pretend the debate is still live.
    with SESSIONS_LOCK:
        live_ids = {sid for sid, st in SESSIONS.items() if st.status in ("starting", "running")}
    out: list[dict] = []
    for d in sorted(sessions_root().iterdir(), reverse=True):
        if not d.is_dir():
            continue
        entry = {"session_id": d.name, "topic": None, "status": "unknown"}
        mf = d / "session.json"
        if mf.exists():
            try:
                data = json.loads(mf.read_text())
                entry.update(
                    {
                        "topic": data.get("topic"),
                        "status": data.get("status"),
                        "moderator_agent": data.get("moderator_agent"),
                        "sides": data.get("sides"),
                        "created_at": data.get("created_at"),
                        "completed_at": data.get("completed_at"),
                        "has_verdict": bool(data.get("verdict_path")),
                    }
                )
                if data.get("status") == "running" and d.name not in live_ids:
                    entry["status"] = "stale"
            except Exception:
                pass
        out.append(entry)
    return out


@app.get("/api/sessions/{sid}")
def show_session(sid: str) -> dict:
    d = sessions_root() / sid
    if not d.is_dir():
        raise HTTPException(404, "no such session")
    result: dict = {"session_id": sid}
    mf = d / "session.json"
    if mf.exists():
        try:
            result["manifest"] = json.loads(mf.read_text())
        except Exception:
            result["manifest"] = None
    verdict = d / "verdict.md"
    if verdict.exists():
        result["verdict"] = verdict.read_text()
    roast = d / "roast.md"
    if roast.exists():
        result["roast"] = roast.read_text()
    chat = d / "chat.jsonl"
    if chat.exists():
        try:
            result["chat"] = [
                json.loads(line) for line in chat.read_text().splitlines() if line.strip()
            ]
        except Exception:
            result["chat"] = []
    result["events"] = read_events(d / "events.jsonl")
    phases: dict[str, dict[str, str]] = {}
    for phase_dir in sorted(d.glob("phase-*")):
        phase_entries: dict[str, str] = {}
        for ans in sorted(phase_dir.glob("*.answer.md")):
            role = ans.name[: -len(".answer.md")]
            try:
                phase_entries[role] = ans.read_text()
            except OSError:
                phase_entries[role] = "(unreadable)"
        if phase_entries:
            phases[phase_dir.name] = phase_entries
    result["phases"] = phases
    return result


@app.get("/api/version")
def get_version() -> dict:
    return {"version": __version__}


@app.get("/api/adapters")
def list_adapters() -> list[str]:
    names = []
    for p in sorted(ADAPTERS_DIR.glob("*.yaml")):
        names.append(p.stem)
    return names


@app.post("/api/debates")
def create_debate(payload: dict) -> dict:
    topic = (payload.get("topic") or "").strip()
    if not topic:
        raise HTTPException(400, "topic required")
    moderator = payload.get("moderator") or "claude"
    sides_in = payload.get("sides") or []
    if len(sides_in) < 2:
        raise HTTPException(400, "at least two sides required")
    sides = []
    for s in sides_in:
        role = (s.get("role") or "").strip()
        stance = (s.get("stance") or "").strip()
        agent = (s.get("agent") or "claude").strip()
        if not role or not stance:
            raise HTTPException(400, "each side needs role and stance")
        sides.append({"role": role, "stance": stance, "agent": agent})
    if len(set(s["role"] for s in sides)) != len(sides):
        raise HTTPException(400, "side roles must be unique")
    if any(s["role"] == "moderator" for s in sides):
        raise HTTPException(400, "'moderator' is a reserved role")

    # Validate adapter names exist.
    adapter_names = {p.stem for p in ADAPTERS_DIR.glob("*.yaml")}
    used = {moderator} | {s["agent"] for s in sides}
    missing = used - adapter_names
    if missing:
        raise HTTPException(400, f"unknown adapters: {sorted(missing)}")

    # Allocate session id up front (same format as the orchestrator) so the
    # caller can use it immediately for the SSE stream.
    from aidebate.core.session import new_session_id

    session_id = new_session_id()
    try:
        crossexam_wallclock = float(payload.get("crossexam_wallclock", 300.0))
        crossexam_silence = float(payload.get("crossexam_silence", 180.0))
    except (TypeError, ValueError):
        raise HTTPException(400, "crossexam_wallclock/silence must be numeric")
    state = SessionState(
        session_id=session_id,
        topic=topic,
        sides=sides,
        moderator_agent=moderator,
        crossexam_wallclock=crossexam_wallclock,
        crossexam_silence=crossexam_silence,
        roast_enabled=bool(payload.get("roast", True)),
    )

    # Because run_debate allocates its own id, we use the state's sid only
    # as a pre-allocated handle while the worker starts; the real debate's
    # session_id lands via `_on_ready`. Store under both for lookup.
    with SESSIONS_LOCK:
        SESSIONS[session_id] = state

    def _start() -> None:
        _run_debate_thread(state)
        # After run_debate completes we also know the real session id.
        if state.debate_session is not None:
            real = state.debate_session.session_id
            with SESSIONS_LOCK:
                SESSIONS[real] = state

    # Poller needs the real session.debate_session. Run both.
    state.thread = threading.Thread(target=_start, daemon=True)
    state.poller = threading.Thread(target=_poll_session, args=(state,), daemon=True)
    state.thread.start()
    state.poller.start()

    # Wait briefly for the real session id to land so we can return it.
    for _ in range(40):
        if state.debate_session is not None:
            break
        time.sleep(0.1)
    real_id = state.debate_session.session_id if state.debate_session else session_id
    with SESSIONS_LOCK:
        SESSIONS[real_id] = state
    return {"session_id": real_id}


@app.get("/api/debates/{sid}")
def get_debate(sid: str) -> JSONResponse:
    state = SESSIONS.get(sid)
    if state is None:
        raise HTTPException(404, "no such debate")
    return JSONResponse(
        {
            "session_id": state.debate_session.session_id
            if state.debate_session
            else state.session_id,
            "topic": state.topic,
            "sides": state.sides,
            "moderator": state.moderator_agent,
            "status": state.status,
            "error": state.error,
            "verdict": state.verdict,
            "roast": state.roast,
            "event_count": len(state.events),
        }
    )


@app.post("/api/debates/{sid}/panes/{role}/keys")
def send_keys_to_pane(sid: str, role: str, payload: dict) -> dict:
    """Send keys / literal text to a running agent's tmux pane.

    Payload shape:
      {"text": "...", "enter": true}   # type text, optionally press Enter
      {"key": "Enter"}                  # send a named tmux key (Enter, C-c, ...)
    """
    state = SESSIONS.get(sid)
    if state is None or state.debate_session is None:
        raise HTTPException(404, "no such debate or not ready yet")
    ap = state.debate_session.panes.get(role)
    if ap is None:
        raise HTTPException(404, f"no pane for role {role!r}")

    text = payload.get("text")
    key = payload.get("key")
    press_enter = bool(payload.get("enter", False))
    if text is None and key is None:
        raise HTTPException(400, "provide 'text' or 'key'")

    if text is not None:
        ap.pane.send_keys(text, enter=False, literal=True)
        if press_enter:
            time.sleep(0.2)
            ap.send_enter()
    elif key is not None:
        ap.send_key(key)
    return {"ok": True}


@app.get("/api/debates/{sid}/events")
async def stream_events(sid: str) -> StreamingResponse:
    state = SESSIONS.get(sid)
    if state is None:
        raise HTTPException(404, "no such debate")

    q = EventBuffer(maxsize=1024)
    # Snapshot both buffers and register the subscriber atomically, so
    # events emitted concurrently by the poller either land entirely in
    # our snapshot (if before the lock) or entirely in our queue (if
    # after the lock added us) — never both, never neither.
    with state._lock:
        narrative_backlog = list(state.narrative_events)
        other_backlog = [ev for ev in state.events if ev.get("type") != "narrative"]
        state.subscribers.append(q)

    async def gen():
        try:
            # Replay the full narrative first so reconnecting clients always
            # see the complete play-by-play, even on long-running debates
            # where pane events have pushed other events out of the
            # trimmed in-memory buffer.
            for ev in narrative_backlog:
                yield f"data: {json.dumps(ev)}\n\n"
            for ev in other_backlog:
                yield f"data: {json.dumps(ev)}\n\n"
            while True:
                try:
                    ev = await asyncio.get_event_loop().run_in_executor(None, q.get, 15.0)
                except queue.Empty:
                    # SSE keep-alive comment.
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(ev)}\n\n"
                if ev.get("type") == "status" and ev.get("status") in ("done", "error"):
                    # Keep the connection alive briefly so the client sees the tail.
                    continue
        finally:
            with state._lock:
                if q in state.subscribers:
                    state.subscribers.remove(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    serve()
