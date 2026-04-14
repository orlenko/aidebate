"use strict";

const el = (id) => document.getElementById(id);
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

let ADAPTERS = [];
let currentEventSource = null;
let CURRENT_SID = null;
let CURRENT_PAYLOAD = null;  // last-submitted payload, used for "Clone"

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

// Fill the setup form from a payload-shaped object.
// Also used by the "Clone" buttons in live/archive views.
function fillFormFrom(payload) {
  $("textarea[name=topic]").value = payload.topic || "";
  el("moderator").value = payload.moderator || payload.moderator_agent || "claude";
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

// ---------------------------------------------------------------------------
// Sessions list + archive view
// ---------------------------------------------------------------------------

async function loadSessions() {
  try {
    const r = await fetch("/api/sessions");
    const list = await r.json();
    const wrap = el("sessions-list");
    if (!list.length) {
      wrap.innerHTML = `<p style="color:var(--muted);font-size:0.9rem">no prior debates yet</p>`;
      return;
    }
    wrap.innerHTML = list.map(s => {
      const sidesHtml = (s.sides || []).map(x =>
        `<span class="sc-side"><code>${escapeHtml(x.role)}</code>${agentIcon(x.agent, {label: false})}</span>`
      ).join("");
      const statusCls = s.status === "done" ? "done"
        : s.status === "running" ? "running"
        : s.status === "error" ? "error" : "idle";
      return `
        <a class="session-card" href="#" data-sid="${escapeHtml(s.session_id)}">
          <div class="sc-head">
            <code>${escapeHtml(s.session_id)}</code>
            <span class="status ${statusCls}">${escapeHtml(s.status || "unknown")}</span>
          </div>
          <div class="sc-topic">${escapeHtml(s.topic || "(no manifest)")}</div>
          <div class="sc-sub">${sidesHtml}</div>
        </a>`;
    }).join("");
    $$(".session-card", wrap).forEach(a => {
      a.addEventListener("click", (e) => {
        e.preventDefault();
        openArchive(a.dataset.sid);
      });
    });
  } catch (e) {
    console.warn("sessions load failed", e);
  }
}

async function openArchive(sid) {
  const r = await fetch(`/api/sessions/${encodeURIComponent(sid)}`);
  if (!r.ok) { alert("failed to load session"); return; }
  const data = await r.json();
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
  el("arc-verdict").innerHTML = data.verdict ? renderMarkdown(data.verdict) : "";
  el("arc-verdict-block").hidden = !data.verdict;

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

  // Stash the manifest as a payload-shaped object so the Clone button can
  // reuse it without re-fetching. Legacy sessions without a manifest get
  // a no-op clone payload (shouldn't happen for new runs).
  el("arc-clone").dataset.payload = JSON.stringify({
    topic: m.topic || "",
    moderator: m.moderator_agent || "claude",
    sides: sides,
  });

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
    enterLiveView(payload, session_id);
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
  el("verdict-block").hidden = true;
  el("verdict-body").textContent = "";
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
      el("verdict-block").hidden = false;
      el("verdict-body").textContent = ev.content;
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
  loadAdapters().then(loadSessions);
  $("#setup-form").addEventListener("submit", submitDebate);
  el("add-side").addEventListener("click", () => addSide({ agent: ADAPTERS[0] }));

  el("new-debate").addEventListener("click", () => {
    if (currentEventSource) currentEventSource.close();
    showSetup();
  });
  el("back-home").addEventListener("click", () => {
    showSetup();
  });

  // Clone from live view — reuse the last payload.
  el("clone-live").addEventListener("click", () => {
    if (!CURRENT_PAYLOAD) return;
    if (currentEventSource) currentEventSource.close();
    fillFormFrom(CURRENT_PAYLOAD);
    showSetup();
  });

  // Clone from archive view — payload JSON is stashed on the button.
  el("arc-clone").addEventListener("click", () => {
    const raw = el("arc-clone").dataset.payload;
    if (!raw) return;
    try {
      fillFormFrom(JSON.parse(raw));
      showSetup();
    } catch (e) { /* ignore */ }
  });
});
