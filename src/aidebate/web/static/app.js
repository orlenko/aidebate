"use strict";

const el = (id) => document.getElementById(id);
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

let ADAPTERS = [];
let currentEventSource = null;

// ---------------------------------------------------------------------------
// Setup phase
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

// Stable color per participant role. Cycles through a small palette so
// the same role always gets the same bubble color within a session.
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
         <td><code>${escapeHtml(s.agent)}</code></td>
         <td>${escapeHtml(s.stance)}</td></tr>`
  ).join("");
  box.innerHTML = `
    <p style="margin:0.3rem 0"><strong>Moderator:</strong> <code>${escapeHtml(payload.moderator || payload.moderator_agent || "")}</code></p>
    <table class="info-table">
      <thead><tr><th>role</th><th>agent</th><th>directions</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

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
      const sides = (s.sides || []).map(x => `${x.role}@${x.agent}`).join(", ");
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
          <div class="sc-sub">${escapeHtml(sides)}</div>
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
  el("arc-moderator").textContent = m.moderator_agent || "?";
  const sides = m.sides || [];
  el("arc-participants").innerHTML = sides.length
    ? `<table class="info-table">
         <thead><tr><th>role</th><th>agent</th><th>directions</th></tr></thead>
         <tbody>${sides.map(s =>
           `<tr><td><code>${escapeHtml(s.role)}</code></td>
                <td><code>${escapeHtml(s.agent)}</code></td>
                <td>${escapeHtml(s.stance)}</td></tr>`).join("")}</tbody>
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

  el("setup").hidden = true;
  el("live").hidden = true;
  el("archive").hidden = false;
}

async function loadAdapters() {
  const r = await fetch("/api/adapters");
  ADAPTERS = await r.json();

  // Populate moderator select.
  const mod = el("moderator");
  mod.innerHTML = "";
  for (const a of ADAPTERS) {
    const opt = document.createElement("option");
    opt.value = a;
    opt.textContent = a;
    mod.appendChild(opt);
  }
  // Default 2 participant rows.
  addSide({ role: "pro", agent: ADAPTERS[0] || "claude", stance: "" });
  addSide({ role: "con", agent: ADAPTERS[0] || "claude", stance: "" });
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

let CURRENT_SID = null;

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
  el("live-topic").textContent = payload.topic;
  el("live-sid").textContent = sessionId;
  el("live-tmux").textContent = `debate-${sessionId}`;
  CURRENT_SID = sessionId;
  renderDebateInfo(payload);
  el("verdict-block").hidden = true;
  el("verdict-body").textContent = "";
  el("event-log").textContent = "";

  // Build debater panes.
  const debaters = el("debaters");
  debaters.innerHTML = "";
  for (const s of payload.sides) {
    const div = document.createElement("div");
    div.className = "pane";
    div.id = `pane-${s.role}`;
    div.innerHTML = `
      <h3 class="pane-title">${s.role} · <span style="color:var(--muted);text-transform:none;font-weight:400">${s.agent}</span></h3>
      <pre class="pane-body">waiting…</pre>
    `;
    div.appendChild(paneControls(s.role));
    debaters.appendChild(div);
  }
  // Reset moderator pane.
  const mod = el("moderator-pane");
  $(".pane-body", mod).textContent = "waiting…";
  $(".pane-title", mod).innerHTML = `moderator · <span style="color:var(--muted);text-transform:none;font-weight:400">${payload.moderator}</span>`;
  // Ensure moderator has a controls row too.
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
  es.onerror = () => {
    // Connection will auto-reconnect; nothing to do.
  };
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

document.addEventListener("DOMContentLoaded", () => {
  loadAdapters().then(loadSessions);
  $("#setup-form").addEventListener("submit", submitDebate);
  el("add-side").addEventListener("click", () => addSide({ agent: ADAPTERS[0] }));
  el("new-debate").addEventListener("click", () => {
    if (currentEventSource) currentEventSource.close();
    el("setup").hidden = false;
    el("live").hidden = true;
    el("archive").hidden = true;
    setStatus("idle", "idle");
    loadSessions();
  });
  el("back-home").addEventListener("click", () => {
    el("archive").hidden = true;
    el("setup").hidden = false;
    loadSessions();
  });
});
