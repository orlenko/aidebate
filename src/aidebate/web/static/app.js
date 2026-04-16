"use strict";

const el = (id) => document.getElementById(id);
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

let ADAPTERS = [];
let currentEventSource = null;
let CURRENT_SID = null;
let CURRENT_PAYLOAD = null;  // last-submitted payload, used for "Clone"

// Play-by-play scroll-lock state for the live view.
let playLogAutoScroll = true;

const STORAGE_KEY = "aidebate:last-form";

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function renderMarkdown(text) {
  if (typeof marked === "undefined") return `<pre>${escapeHtml(text || "")}</pre>`;
  try {
    return marked.parse(text || "", { breaks: true, gfm: true });
  } catch (e) {
    return `<pre>${escapeHtml(text || "")}</pre>`;
  }
}

// Agent icon — a small circular chip with a letter/glyph. Colors live in
// CSS (.agent-chip.agent-<name>); new adapters get a generic fallback.
const AGENT_GLYPHS = {
  claude: "C",
  gemini: "G",
  codex: "</>",
};
function agentIcon(name, opts = {}) {
  const glyph = AGENT_GLYPHS[name] || (name || "?").slice(0, 1).toUpperCase();
  const cls = `agent-chip agent-${escapeHtml(name || "unknown")}`;
  const label = opts.label === false ? "" :
    `<span class="agent-name">${escapeHtml(name || "")}</span>`;
  return `<span class="agent" title="${escapeHtml(name || "")}">
            <span class="${cls}">${escapeHtml(glyph)}</span>${label}
          </span>`;
}

// Stable color per participant role.
const CHAT_PALETTE = [
  "#7cc4ff", "#b48aff", "#8fd18f", "#ffb170",
  "#ff8fa8", "#7ee6d4", "#d6c87a", "#9bb4ff",
];
function colorForRole(role) {
  let h = 0;
  for (const c of role || "") h = (h * 31 + c.charCodeAt(0)) & 0xffffffff;
  return CHAT_PALETTE[Math.abs(h) % CHAT_PALETTE.length];
}

function renderChat(messages) {
  if (!messages || !messages.length) return `<p class="muted">(empty)</p>`;
  return messages.map(m => {
    const color = colorForRole(m.from);
    const ts = (m.ts || "").replace(/\.\d+Z$/, "Z");
    const to = (m.to || []).join(", ") || "*";
    return `
      <div class="chat-msg">
        <div class="chat-head">
          <span class="chat-from" style="color:${color}"><strong>${escapeHtml(m.from || "?")}</strong></span>
          <span class="chat-to">→ ${escapeHtml(to)}</span>
          <span class="chat-ts">${escapeHtml(ts)}</span>
        </div>
        <div class="chat-body">${escapeHtml(m.text || "")}</div>
      </div>`;
  }).join("");
}

function renderDebateInfo(payload) {
  const box = el("debate-info-body");
  const rows = (payload.sides || []).map(s =>
    `<tr><td><code>${escapeHtml(s.role)}</code></td>
         <td>${agentIcon(s.agent)}</td>
         <td>${escapeHtml(s.stance)}</td></tr>`
  ).join("");
  const modAgent = payload.moderator || payload.moderator_agent || "";
  box.innerHTML = `
    <p style="margin:0.3rem 0"><strong>Moderator:</strong> ${agentIcon(modAgent)}</p>
    <table class="info-table">
      <thead><tr><th>role</th><th>agent</th><th>directions</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

// ---------------------------------------------------------------------------
// Play-by-play renderer — turns a `narrative` event into a log line.
// ---------------------------------------------------------------------------

const PHASE_LABELS = {
  opening: "Phase 1 — Opening statements",
  crossexam: "Phase 2 — Cross-examination",
  rebuttal: "Phase 3 — Rebuttals",
  verdict: "Phase 4 — Verdict",
  roast: "Phase 5 — Roast",
};

function fmtEventTime(isoTs) {
  if (!isoTs) return "";
  try {
    const d = new Date(isoTs);
    if (isNaN(d.getTime())) return isoTs;
    return d.toLocaleTimeString([], { hour12: false });
  } catch { return isoTs; }
}

// Given a narrative event, return { icon, bodyHtml, cls } or null to skip.
function formatNarrative(ev) {
  const roleTag = (r) => `<span class="role" style="color:${colorForRole(r)}">${escapeHtml(r)}</span>`;
  switch (ev.type) {
    case "debate_started": {
      const sides = (ev.sides || []).map(s => `${roleTag(s.role)} (${escapeHtml(s.agent)})`).join(", ");
      return {
        icon: "🏁",
        cls: "ev-phase",
        bodyHtml: `Debate started — topic: <em>${escapeHtml(ev.topic || "")}</em>. Moderator: <code>${escapeHtml(ev.moderator_agent || "?")}</code>. Debaters: ${sides || "(none)"}.`,
      };
    }
    case "canary_started":
      return { icon: "🛎️", bodyHtml: `Canary handshake…` };
    case "participant_ready":
      return {
        icon: "✓",
        bodyHtml: `${roleTag(ev.role)} <span class="muted">(${escapeHtml(ev.agent || "?")})</span> ready`,
      };
    case "dropout":
      return {
        icon: "⚠",
        cls: "ev-dropout",
        bodyHtml: `${roleTag(ev.role)} dropped during <strong>${escapeHtml(ev.phase || "?")}</strong>: <span class="muted">${escapeHtml(ev.error || "")}</span>`,
      };
    case "phase_started":
      return {
        icon: "▶",
        cls: "ev-phase",
        bodyHtml: PHASE_LABELS[ev.phase] || `Phase: ${escapeHtml(ev.phase || "?")}`,
      };
    case "phase_completed":
      return {
        icon: "✓",
        bodyHtml: `<span class="muted">${escapeHtml(PHASE_LABELS[ev.phase] || ev.phase || "phase")} complete</span>`,
      };
    case "phase_skipped":
      return {
        icon: "↷",
        bodyHtml: `<span class="muted">${escapeHtml(PHASE_LABELS[ev.phase] || ev.phase || "phase")} skipped — ${escapeHtml(ev.reason || "")}</span>`,
      };
    case "participant_completed_phase":
      return {
        icon: "✓",
        bodyHtml: `${roleTag(ev.role)} finished ${escapeHtml(ev.phase || "")}`,
      };
    case "chat_message": {
      const to = Array.isArray(ev.to) ? ev.to.join(", ") : (ev.to || "*");
      return {
        icon: "💬",
        bodyHtml: `${roleTag(ev.from || "?")} → ${escapeHtml(to || "*")}: <span class="chat-text">${escapeHtml(ev.text || "")}</span>`,
      };
    }
    case "verdict_ready":
      return { icon: "⚖", bodyHtml: `Verdict ready.` };
    case "roast_ready":
      return { icon: "🔥", bodyHtml: `Roast ready.` };
    case "debate_completed":
      if (ev.status === "error") {
        return {
          icon: "✖",
          cls: "ev-dropout",
          bodyHtml: `Debate ended with error: <span class="muted">${escapeHtml(ev.error || "")}</span>`,
        };
      }
      return { icon: "🏆", cls: "ev-done", bodyHtml: `Debate complete.` };
    default:
      return null;
  }
}

function appendNarrativeToLog(logEl, ev) {
  const formatted = formatNarrative(ev);
  if (!formatted) return;
  const li = document.createElement("li");
  if (formatted.cls) li.className = formatted.cls;
  li.innerHTML = `
    <span class="ev-ts">${escapeHtml(fmtEventTime(ev.ts))}</span>
    <span class="ev-icon">${formatted.icon || ""}</span>
    <span class="ev-body">${formatted.bodyHtml}</span>`;
  // Drop the "waiting" placeholder the first time we append.
  const placeholder = $(".ev-empty", logEl);
  if (placeholder) placeholder.remove();
  logEl.appendChild(li);
}

function setupPlayLogScrollLock() {
  const log = el("play-log");
  const btn = el("jump-live");
  if (!log || !btn) return;
  playLogAutoScroll = true;
  btn.hidden = true;
  log.onscroll = () => {
    // "at bottom" with a 40px slack for sub-pixel rounding.
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    if (atBottom) {
      playLogAutoScroll = true;
      btn.hidden = true;
    } else {
      playLogAutoScroll = false;
      btn.hidden = false;
    }
  };
  btn.onclick = () => {
    playLogAutoScroll = true;
    btn.hidden = true;
    log.scrollTop = log.scrollHeight;
  };
}

function maybeAutoScroll() {
  if (!playLogAutoScroll) return;
  const log = el("play-log");
  if (log) log.scrollTop = log.scrollHeight;
}

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------

function bindTabs(tabsRoot, panelPrefix, dataAttr = "data-tab") {
  const buttons = $$(`.tab[${dataAttr}]`, tabsRoot);
  buttons.forEach(btn => {
    btn.addEventListener("click", () => {
      const tab = btn.getAttribute(dataAttr);
      buttons.forEach(b => {
        const active = b === btn;
        b.classList.toggle("active", active);
        b.setAttribute("aria-selected", active ? "true" : "false");
      });
      // Panels are siblings after the tab bar.
      const panels = tabsRoot.parentElement.querySelectorAll(".tab-panel");
      panels.forEach(p => {
        p.classList.toggle("active", p.id === `${panelPrefix}${tab}`);
      });
      if (tab === "play") maybeAutoScroll();
    });
  });
}

function activateTab(tabsRoot, tabName, dataAttr = "data-tab") {
  const btn = $(`.tab[${dataAttr}="${tabName}"]`, tabsRoot);
  if (btn) btn.click();
}

// ---------------------------------------------------------------------------
// Form persistence
// ---------------------------------------------------------------------------

function saveForm(payload) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
  } catch (e) { /* quota or private mode — ignore */ }
}

function loadSavedForm() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const p = JSON.parse(raw);
    if (!p || !Array.isArray(p.sides) || p.sides.length < 2) return null;
    return p;
  } catch (e) { return null; }
}

function fillFormFrom(payload) {
  $("textarea[name=topic]").value = payload.topic || "";
  el("moderator").value = payload.moderator || payload.moderator_agent || "claude";
  el("no-roast").checked = payload.roast === false;
  el("sides").innerHTML = "";
  for (const s of payload.sides || []) addSide(s);
  if ((payload.sides || []).length < 2) {
    while ($$("#sides .side-row").length < 2) {
      addSide({ role: "", agent: ADAPTERS[0] || "claude", stance: "" });
    }
  }
}

function showSetup() {
  el("setup").hidden = false;
  el("live").hidden = true;
  el("archive").hidden = true;
  setStatus("idle", "idle");
  window.scrollTo({ top: 0, behavior: "smooth" });
  loadSessions();
}

function navigate(path) {
  if (location.hash === path) router();
  else location.hash = path;
}

function currentRoute() {
  const m = (location.hash || "").match(/^#\/(live|archive)\/(.+)$/);
  if (!m) return { view: "setup" };
  return { view: m[1], sid: decodeURIComponent(m[2]) };
}

async function router() {
  const route = currentRoute();
  if (route.view === "live") {
    if (CURRENT_PAYLOAD && CURRENT_SID === route.sid) {
      enterLiveView(CURRENT_PAYLOAD, route.sid);
      return;
    }
    try {
      const data = await _fetchSession(route.sid);
      const m = data.manifest || {};
      const payload = {
        topic: m.topic || "",
        moderator: m.moderator_agent || "claude",
        sides: m.sides || [],
        roast: m.roast_enabled !== false,
      };
      enterLiveView(payload, route.sid);
    } catch (e) {
      alert("couldn't open live view: " + e);
      navigate("#/");
    }
  } else if (route.view === "archive") {
    await openArchive(route.sid);
  } else {
    if (currentEventSource) {
      currentEventSource.close();
      currentEventSource = null;
    }
    showSetup();
  }
}

// ---------------------------------------------------------------------------
// Sessions list + archive view
// ---------------------------------------------------------------------------

function _renderSessionCard(s) {
  const sidesHtml = (s.sides || []).map(x =>
    `<span class="sc-side"><code>${escapeHtml(x.role)}</code>${agentIcon(x.agent, {label: false})}</span>`
  ).join("");
  const statusCls = s.status === "done" ? "done"
    : s.status === "running" || s.status === "starting" ? "running"
    : s.status === "error" ? "error"
    : s.status === "stale" ? "error"
    : "idle";
  const href = (s.status === "running" || s.status === "starting")
    ? `#/live/${encodeURIComponent(s.session_id)}`
    : `#/archive/${encodeURIComponent(s.session_id)}`;
  return `
    <a class="session-card" href="${href}" data-sid="${escapeHtml(s.session_id)}">
      <div class="sc-head">
        <code>${escapeHtml(s.session_id)}</code>
        <span class="status ${statusCls}">${escapeHtml(s.status || "unknown")}</span>
      </div>
      <div class="sc-topic">${escapeHtml(s.topic || "(no manifest)")}</div>
      <div class="sc-sub">${sidesHtml}</div>
    </a>`;
}

async function loadSessions() {
  try {
    const r = await fetch("/api/sessions");
    const list = await r.json();
    const running = list.filter(s => s.status === "running" || s.status === "starting");
    const others = list.filter(s => !(s.status === "running" || s.status === "starting"));

    const runningWrap = el("running-now");
    if (running.length) {
      runningWrap.hidden = false;
      el("running-list").innerHTML = running.map(_renderSessionCard).join("");
    } else {
      runningWrap.hidden = true;
      el("running-list").innerHTML = "";
    }

    const wrap = el("sessions-list");
    if (!others.length) {
      wrap.innerHTML = `<p style="color:var(--muted);font-size:0.9rem">no prior debates yet</p>`;
    } else {
      wrap.innerHTML = others.map(_renderSessionCard).join("");
    }
  } catch (e) {
    console.warn("sessions load failed", e);
  }
}

async function _fetchSession(sid) {
  const r = await fetch(`/api/sessions/${encodeURIComponent(sid)}`);
  if (!r.ok) throw new Error(`failed to load session ${sid}: ${r.status}`);
  return r.json();
}

async function openArchive(sid) {
  let data;
  try { data = await _fetchSession(sid); }
  catch (e) { alert("failed to load session"); return; }
  const m = data.manifest || {};
  el("arc-sid").textContent = data.session_id;
  el("arc-topic").textContent = m.topic || "(no topic)";
  el("arc-status").textContent = m.status || "unknown";
  el("arc-moderator").innerHTML = agentIcon(m.moderator_agent || "");
  const sides = m.sides || [];
  const droppedRoles = new Set((m.dropouts || []).map(d => d.role));
  el("arc-participants").innerHTML = sides.length
    ? `<table class="info-table">
         <thead><tr><th>role</th><th>agent</th><th>directions</th></tr></thead>
         <tbody>${sides.map(s => {
           const dropped = droppedRoles.has(s.role);
           const drop = (m.dropouts || []).find(d => d.role === s.role);
           const droppedBadge = dropped
             ? `<span class="dropout" title="${escapeHtml(drop ? drop.phase + ': ' + drop.error : 'dropped out')}">dropped @ ${escapeHtml(drop ? drop.phase : '?')}</span>`
             : "";
           return `<tr${dropped ? ' class="dropped"' : ''}>
                     <td><code>${escapeHtml(s.role)}</code> ${droppedBadge}</td>
                     <td>${agentIcon(s.agent)}</td>
                     <td>${escapeHtml(s.stance)}</td>
                   </tr>`;
         }).join("")}</tbody>
       </table>`
    : "";
  el("arc-verdict").innerHTML = data.verdict ? renderMarkdown(data.verdict) : "<p class='muted'>(no verdict on file)</p>";
  el("arc-roast").innerHTML = data.roast ? renderMarkdown(data.roast) : "<p class='muted'>(no roast on file)</p>";

  // Hide the roast tab if this debate never had one.
  const archiveRoot = el("archive");
  const roastTabBtn = $(`.tab[data-arc-tab="roast"]`, archiveRoot);
  if (roastTabBtn) roastTabBtn.hidden = !data.roast;

  // Render play-by-play from events.jsonl; fall back to a notice for old
  // sessions that predate event emission.
  const playLog = el("arc-play-log");
  playLog.innerHTML = "";
  const events = data.events || [];
  if (!events.length) {
    playLog.innerHTML = `<li class="ev-empty">No play-by-play recorded for this debate (pre-v0.5 session, or events file missing).</li>`;
  } else {
    for (const ev of events) appendNarrativeToLog(playLog, ev);
  }

  const phases = data.phases || {};
  el("arc-phases").innerHTML = Object.keys(phases).sort().map(ph => {
    const byRole = phases[ph];
    return `<h4>${escapeHtml(ph)}</h4>` + Object.keys(byRole).sort().map(role => {
      const color = colorForRole(role);
      return `<details>
         <summary class="phase-summary">
           <span class="phase-role" style="color:${color}">${escapeHtml(role)}</span>
         </summary>
         <div class="md">${renderMarkdown(byRole[role])}</div>
       </details>`;
    }).join("");
  }).join("") || "<p>(no phase answers yet)</p>";

  el("arc-chat").innerHTML = renderChat(data.chat || []);

  el("arc-clone").dataset.payload = JSON.stringify({
    topic: m.topic || "",
    moderator: m.moderator_agent || "claude",
    sides: sides,
    roast: m.roast_enabled !== false,
  });

  // Reset tab state to play-by-play on each archive open.
  activateTab($(".tabs", archiveRoot), "play", "data-arc-tab");

  el("setup").hidden = true;
  el("live").hidden = true;
  el("archive").hidden = false;
}

// ---------------------------------------------------------------------------
// Setup form
// ---------------------------------------------------------------------------

async function loadAdapters() {
  const r = await fetch("/api/adapters");
  ADAPTERS = await r.json();

  const mod = el("moderator");
  mod.innerHTML = "";
  for (const a of ADAPTERS) {
    const opt = document.createElement("option");
    opt.value = a;
    opt.textContent = a;
    mod.appendChild(opt);
  }

  const saved = loadSavedForm();
  if (saved) {
    fillFormFrom(saved);
  } else {
    addSide({ role: "pro", agent: ADAPTERS[0] || "claude", stance: "" });
    addSide({ role: "con", agent: ADAPTERS[0] || "claude", stance: "" });
  }
}

function addSide(preset = {}) {
  const wrap = document.createElement("div");
  wrap.className = "side-row";
  wrap.innerHTML = `
    <input type="text" name="role" placeholder="role (e.g. pro)" required />
    <select name="agent"></select>
    <textarea name="stance" rows="2" placeholder="Directions for this debater — their stance and any special instructions." required></textarea>
    <button type="button" class="remove" title="remove">×</button>
  `;
  $("input[name=role]", wrap).value = preset.role || "";
  $("textarea[name=stance]", wrap).value = preset.stance || "";
  const sel = $("select[name=agent]", wrap);
  for (const a of ADAPTERS) {
    const opt = document.createElement("option");
    opt.value = a;
    opt.textContent = a;
    sel.appendChild(opt);
  }
  if (preset.agent) sel.value = preset.agent;
  $("button.remove", wrap).addEventListener("click", () => {
    if ($$("#sides .side-row").length > 2) wrap.remove();
  });
  el("sides").appendChild(wrap);
}

function collectSides() {
  return $$("#sides .side-row").map((row) => ({
    role: $("input[name=role]", row).value.trim(),
    agent: $("select[name=agent]", row).value,
    stance: $("textarea[name=stance]", row).value.trim(),
  }));
}

async function submitDebate(ev) {
  ev.preventDefault();
  const payload = {
    topic: $("textarea[name=topic]").value.trim(),
    moderator: el("moderator").value,
    sides: collectSides(),
    roast: !el("no-roast").checked,
  };
  saveForm(payload);
  setStatus("starting", "starting…");
  try {
    const r = await fetch("/api/debates", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!r.ok) {
      const err = await r.text();
      setStatus("error", "error");
      alert("Failed to start: " + err);
      return;
    }
    const { session_id } = await r.json();
    CURRENT_PAYLOAD = payload;
    CURRENT_SID = session_id;
    navigate(`#/live/${encodeURIComponent(session_id)}`);
  } catch (e) {
    setStatus("error", "error");
    alert("Network error: " + e);
  }
}

// ---------------------------------------------------------------------------
// Live view
// ---------------------------------------------------------------------------

async function sendKeysTo(role, body) {
  if (!CURRENT_SID) return;
  try {
    await fetch(`/api/debates/${CURRENT_SID}/panes/${role}/keys`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (e) {
    console.warn("send keys failed", e);
  }
}

function paneControls(role) {
  const wrap = document.createElement("div");
  wrap.className = "pane-controls";
  wrap.innerHTML = `
    <input type="text" class="pc-text" placeholder="type to send (Enter submits)" />
    <button type="button" class="pc-enter" title="send Enter">⏎</button>
    <button type="button" class="pc-ctrlc" title="send Ctrl-C">^C</button>
  `;
  const input = $(".pc-text", wrap);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      const text = input.value;
      input.value = "";
      sendKeysTo(role, { text, enter: true });
    }
  });
  $(".pc-enter", wrap).addEventListener("click", () => {
    sendKeysTo(role, { key: "Enter" });
  });
  $(".pc-ctrlc", wrap).addEventListener("click", () => {
    sendKeysTo(role, { key: "C-c" });
  });
  return wrap;
}

function enterLiveView(payload, sessionId) {
  el("setup").hidden = true;
  el("live").hidden = false;
  el("archive").hidden = true;
  el("live-topic").textContent = payload.topic;
  el("live-sid").textContent = sessionId;
  el("live-tmux").textContent = `debate-${sessionId}`;
  CURRENT_SID = sessionId;
  CURRENT_PAYLOAD = payload;
  renderDebateInfo(payload);

  // Reset the play-by-play log and tab state.
  el("play-log").innerHTML = `<li class="ev-empty">waiting for the debate to begin…</li>`;
  el("verdict-body-md").innerHTML = `<p class="muted">waiting for verdict…</p>`;
  el("verdict-ready-pill").hidden = true;
  el("roast-ready-pill").hidden = true;
  el("roast-body").innerHTML = `<p class="muted">waiting for roast…</p>`;
  // Hide the roast tab if roast is disabled for this debate.
  const liveRoot = el("live");
  const roastTabBtn = $(`.tab[data-tab="roast"]`, liveRoot);
  if (roastTabBtn) roastTabBtn.hidden = payload.roast === false;

  activateTab($(".tabs", liveRoot), "play", "data-tab");
  setupPlayLogScrollLock();

  el("event-log").textContent = "";

  const debaters = el("debaters");
  debaters.innerHTML = "";
  for (const s of payload.sides) {
    const div = document.createElement("div");
    div.className = "pane";
    div.id = `pane-${s.role}`;
    div.innerHTML = `
      <h3 class="pane-title">${escapeHtml(s.role)} ${agentIcon(s.agent)}</h3>
      <pre class="pane-body">waiting…</pre>
    `;
    div.appendChild(paneControls(s.role));
    debaters.appendChild(div);
  }
  if (payload.roast !== false) {
    const roastDiv = document.createElement("div");
    roastDiv.className = "pane";
    roastDiv.id = "pane-roastmaster";
    roastDiv.innerHTML = `
      <h3 class="pane-title">🔥 roastmaster ${agentIcon("claude")}</h3>
      <pre class="pane-body">idle — waiting for verdict…</pre>
    `;
    roastDiv.appendChild(paneControls("roastmaster"));
    debaters.appendChild(roastDiv);
  }
  const mod = el("moderator-pane");
  $(".pane-body", mod).textContent = "waiting…";
  $(".pane-title", mod).innerHTML = `moderator ${agentIcon(payload.moderator)}`;
  const existingCtrl = $(".pane-controls", mod);
  if (existingCtrl) existingCtrl.remove();
  mod.appendChild(paneControls("moderator"));

  subscribe(sessionId);
}

function subscribe(sessionId) {
  if (currentEventSource) currentEventSource.close();
  const es = new EventSource(`/api/debates/${sessionId}/events`);
  currentEventSource = es;
  es.onmessage = (m) => {
    let ev;
    try { ev = JSON.parse(m.data); } catch { return; }
    handleEvent(ev);
    logEvent(ev);
  };
  es.onerror = () => {};
}

function handleEvent(ev) {
  switch (ev.type) {
    case "status":
      setStatus(ev.status, ev.status);
      if (ev.status === "done" || ev.status === "error") {
        loadSessions();
      }
      break;
    case "session_ready":
      el("live-tmux").textContent = ev.tmux;
      break;
    case "pane": {
      const pane = el(`pane-${ev.role}`) || (ev.role === "moderator" ? el("moderator-pane") : null);
      if (!pane) return;
      const body = $(".pane-body", pane);
      body.textContent = ev.text;
      body.scrollTop = body.scrollHeight;
      break;
    }
    case "verdict":
      el("verdict-body-md").innerHTML = renderMarkdown(ev.content || "");
      el("verdict-ready-pill").hidden = false;
      break;
    case "roast":
      el("roast-body").innerHTML = renderMarkdown(ev.content || "");
      el("roast-ready-pill").hidden = false;
      break;
    case "narrative":
      if (ev.event) {
        appendNarrativeToLog(el("play-log"), ev.event);
        maybeAutoScroll();
      }
      break;
    case "error":
      setStatus("error", "error: " + (ev.message || ""));
      break;
  }
}

function logEvent(ev) {
  const t = new Date((ev.ts || 0) * 1000).toLocaleTimeString();
  const summary = ev.type === "pane"
    ? `${t}  pane[${ev.role}] (${(ev.text || "").length} chars)`
    : ev.type === "answer"
    ? `${t}  answer[${ev.phase}/${ev.role}] (${(ev.content || "").length} chars)`
    : ev.type === "narrative"
    ? `${t}  narrative:${ev.event && ev.event.type}`
    : `${t}  ${ev.type} ${JSON.stringify({ ...ev, id: undefined, ts: undefined, type: undefined })}`;
  const log = el("event-log");
  log.textContent += summary + "\n";
  log.scrollTop = log.scrollHeight;
}

function setStatus(cls, text) {
  const s = el("status");
  s.className = "status " + cls;
  s.textContent = text;
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

async function loadVersion() {
  try {
    const r = await fetch("/api/version");
    const { version } = await r.json();
    if (version) el("version").textContent = "v" + version;
  } catch (e) { /* ignore */ }
}

document.addEventListener("DOMContentLoaded", () => {
  loadVersion();
  $("#setup-form").addEventListener("submit", submitDebate);
  el("add-side").addEventListener("click", () => addSide({ agent: ADAPTERS[0] }));

  // Wire up the tab bars once — they're in the DOM from page load.
  const liveTabs = $("#live .tabs");
  if (liveTabs) bindTabs(liveTabs, "tab-", "data-tab");
  const arcTabs = $("#archive .tabs");
  if (arcTabs) bindTabs(arcTabs, "arc-tab-", "data-arc-tab");

  const goHome = () => navigate("#/");
  el("new-debate").addEventListener("click", goHome);
  el("back-home").addEventListener("click", goHome);
  el("live-back").addEventListener("click", goHome);

  el("clone-live").addEventListener("click", () => {
    if (!CURRENT_PAYLOAD) return;
    fillFormFrom(CURRENT_PAYLOAD);
    navigate("#/");
  });

  el("arc-clone").addEventListener("click", () => {
    const raw = el("arc-clone").dataset.payload;
    if (!raw) return;
    try {
      fillFormFrom(JSON.parse(raw));
      navigate("#/");
    } catch (e) { /* ignore */ }
  });

  window.addEventListener("hashchange", router);

  loadAdapters().then(() => { loadSessions(); router(); });
});
