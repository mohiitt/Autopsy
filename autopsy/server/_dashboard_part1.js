// autopsy dashboard — vanilla JS fallback UI
// Talks to the FastAPI server: REST + WS.

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  sessions: [],
  activeSessionId: null,
  activeNodeId: null,
  liveEvents: [],          // events accumulated for in-progress session
  liveSessionId: null,     // session_id currently streaming
  bundleCache: {},         // session_id -> full bundle
  diagnosis: null,
  diagnosisLoading: false,
  replayResult: null,
  ws: null,
  wsReconnectAttempts: 0,
  activeTab: "overview",
};

function toast(msg, level = "info") {
  const el = document.createElement("div");
  el.className = "toast" + (level === "err" ? " err" : "");
  el.textContent = msg;
  document.getElementById("toast-container").appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

function setWsStatus(connected) {
  const el = $("#ws-status");
  if (!el) return;
  el.classList.toggle("live", connected);
  el.classList.toggle("dead", !connected);
  el.textContent = connected ? "live" : "offline";
}

function connectWS() {
  try {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}/ws/live`;
    const ws = new WebSocket(url);
    state.ws = ws;
    ws.onopen = () => {
      setWsStatus(true);
      state.wsReconnectAttempts = 0;
    };
    ws.onclose = () => {
      setWsStatus(false);
      const delay = Math.min(5000, 500 * 2 ** state.wsReconnectAttempts);
      state.wsReconnectAttempts++;
      setTimeout(connectWS, delay);
    };
    ws.onerror = () => {
      try { ws.close(); } catch (e) {}
    };
    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        handleWsMessage(msg);
      } catch (err) {
        console.error("ws message parse failed", err);
      }
    };
  } catch (e) {
    console.error("ws connect failed", e);
    setTimeout(connectWS, 1500);
  }
}

function handleWsMessage(msg) {
  if (msg.type === "sessions_list") {
    state.sessions = msg.data || [];
    renderSessions();
    return;
  }
  if (msg.type === "event") {
    const ev = msg.data || {};
    // Track new session.
    if (ev.event_type === "session_start") {
      state.liveSessionId = ev.session_id;
      state.liveEvents = [];
      // Insert at top of sidebar.
      state.sessions = [{
        session_id: ev.session_id,
        agent_name: ev.agent_name || "running...",
        created_at: ev.timestamp || Date.now() / 1000,
        status: "running",
        error_count: 0,
        node_count: 0,
        input_query: ev.input_query || "",
      }, ...state.sessions.filter(s => s.session_id !== ev.session_id)];
      renderSessions();
      // Auto-focus the new live session.
      selectSession(ev.session_id, /*isLive*/ true);
    }
    if (ev.session_id === state.liveSessionId) {
      state.liveEvents.push(ev);
      // Update sidebar counts for the live session.
      const live = state.sessions.find(s => s.session_id === state.liveSessionId);
      if (live) {
        if (ev.event_type === "node_start") live.node_count = (live.node_count || 0) + 1;
        if (ev.event_type === "node_error") live.error_count = (live.error_count || 0) + 1;
        renderSessions();
      }
      if (state.activeSessionId === state.liveSessionId) {
        renderCenter();
      }
    }
    return;
  }
  if (msg.type === "session_complete") {
    const summary = msg.data || {};
    state.liveSessionId = null;
    // Refresh sessions list from API.
    fetchSessions();
    // If the user was viewing this live session, refetch bundle.
    if (state.activeSessionId === summary.session_id) {
      fetchBundle(summary.session_id).then(() => renderCenter());
    }
    return;
  }
  if (msg.type === "demo_status") {
    state.demoFixed = !!(msg.data && msg.data.fix_applied);
    if (typeof renderHeaderStatus === "function") renderHeaderStatus();
    return;
  }
}

async function fetchSessions() {
  try {
    const r = await fetch("/api/sessions");
    const data = await r.json();
    if (Array.isArray(data)) {
      // Preserve live session at top if still running.
      const live = state.sessions.find(
        s => s.session_id === state.liveSessionId);
      state.sessions = live ? [live, ...data.filter(
        s => s.session_id !== live.session_id)] : data;
      renderSessions();
    }
  } catch (e) {
    console.error("fetchSessions failed", e);
  }
}

async function fetchBundle(sessionId) {
  if (state.bundleCache[sessionId]) return state.bundleCache[sessionId];
  try {
    const r = await fetch(`/api/sessions/${sessionId}`);
    if (!r.ok) throw new Error(`status ${r.status}`);
    const data = await r.json();
    state.bundleCache[sessionId] = data;
    return data;
  } catch (e) {
    toast(`Failed to load session: ${e.message}`, "err");
    return null;
  }
}

function renderSessions() {
  const root = $("#session-list");
  // Always show the header with the "clear" button so users can purge noise.
  let header = `<div class="trace-header">
    <span>TRACES (${state.sessions.length})</span>
    <button class="btn ghost btn-xs" id="btn-clear-traces" title="Delete all finished traces">clear</button>
  </div>`;
  if (!state.sessions.length) {
    root.innerHTML = header + `<div class="empty">no traces yet.<br/><br/>
      run <code>autopsy run agent.py</code></div>`;
    bindClearTraces();
    return;
  }
  root.innerHTML = header + state.sessions.map(s => {
    const dt = new Date((s.created_at || 0) * 1000);
    const time = isNaN(dt) ? "" : dt.toLocaleTimeString();
    const active = s.session_id === state.activeSessionId ? " active" : "";
    const isLive = s.session_id === state.liveSessionId;
    const status = isLive ? "running" : (s.status || "unknown");
    const liveBadge = isLive ? ' <span class="live-badge">LIVE</span>' : '';
    return `<div class="session${active}${isLive ? ' live' : ''}" data-id="${s.session_id}">
      <div class="name"><span class="status ${status}"></span>${escapeHtml(s.agent_name || "agent")}${liveBadge}</div>
      <div class="meta">${time} · ${s.node_count || 0} nodes · ${s.error_count || 0} errors</div>
    </div>`;
  }).join("");
  $$(".session", root).forEach(el => {
    el.addEventListener("click",
      () => selectSession(el.dataset.id));
  });
  bindClearTraces();
}

function bindClearTraces() {
  const btn = document.getElementById("btn-clear-traces");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    if (!confirm("Delete all finished traces? (the live run is kept)")) return;
    try {
      const r = await fetch("/api/sessions?keep_live=1", { method: "DELETE" });
      if (r.ok) {
        const data = await r.json();
        state.sessions = state.sessions.filter(
          s => s.session_id === state.liveSessionId);
        // Clear cached bundles and active selection if it was deleted.
        if (state.activeSessionId !== state.liveSessionId) {
          state.activeSessionId = state.liveSessionId;
        }
        renderSessions();
        renderCenter();
        toast(`Cleared ${data.deleted} trace(s)`);
      } else {
        toast("Clear failed", "err");
      }
    } catch (e) {
      toast(`Clear failed: ${e.message}`, "err");
    }
  });
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;"
  }[c]));
}
