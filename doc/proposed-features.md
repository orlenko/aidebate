# Proposed features

Parking lot for ideas we want to remember but aren't building yet. Prune freely.

## Interactive moderator / live debate steering

Let the user steer a debate *while it's running* from the web UI:

- Inject a question mid-round ("hey Moderator, push harder on point 3")
- Pause / resume a phase
- Kick a stuck or misbehaving agent and either skip it or respawn
- Nudge the roastmaster with a target ("roast the loser extra hard")

**Why it's interesting:** today aidebate is fire-and-forget. The user watches. Making it interactive turns it from "AI debate as spectator sport" into "AI debate as collaborative tool" — the human becomes the real moderator, Claude-moderator becomes the scribe.

**Why it's deferred:** the current one-way SSE event stream is the right tool for passive viewing, and upgrading to bidirectional (WebSockets, or SSE + a POST control channel) is only worth it once we actually want this. Needs:

- Control protocol: what commands, what state transitions are legal?
- UI affordances: pause button, inject-message box, kick-agent menu per debater
- Pipeline hooks: `core/debate.py` and `core/crossexam.py` need injection points that can accept commands between phases/turns
- Auth/safety: if the server is ever exposed beyond localhost, controls need gating

**Probable approach when we do it:** keep SSE for events, add `POST /api/debates/{sid}/control` for commands. Don't reach for WebSockets unless something actually needs continuous bidirectional traffic.
