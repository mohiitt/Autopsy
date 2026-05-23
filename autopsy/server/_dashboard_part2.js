async function selectSession(sessionId, isLive = false) {
  state.activeSessionId = sessionId;
  state.activeNodeId = null;
  state.diagnosis = null;
  state.replayResult = null;
  renderSessions();
  // Live session uses in-memory liveEvents; finished session loads from API.
  if (!isLive && sessionId !== state.liveSessionId) {
    await fetchBundle(sessionId);
  }
  renderCenter();
  renderDetail();
}

function getActiveBundleLike() {
  // Returns a unified bundle-shaped object for the active session,
  // whether live or loaded.
  const sid = state.activeSessionId;
  if (!sid) return null;
  if (sid === state.liveSessionId) {
    // Build a synthetic bundle from liveEvents.
    return buildLiveBundle();
  }
  return state.bundleCache[sid] || null;
}

function buildLiveBundle() {
  const events = state.liveEvents.slice();
  const node_index = {};
  const dag_edges = [];
  for (const e of events) {
    const t = e.event_type;
    const nid = e.node_id;
    if (!nid && t !== "session_start" && t !== "session_end") continue;
    if (t === "node_start") {
      node_index[nid] = node_index[nid] || {};
      node_index[nid].start_event = e;
      if (e.parent_node_id) dag_edges.push([e.parent_node_id, nid]);
    } else if (t === "node_end") {
      node_index[nid] = node_index[nid] || {};
      node_index[nid].end_event = e;
    } else if (t === "node_error") {
      node_index[nid] = node_index[nid] || {};
      node_index[nid].error_event = e;
    } else if (t === "llm_request" || t === "llm_response") {
      node_index[nid] = node_index[nid] || {};
      node_index[nid].llm_events = node_index[nid].llm_events || [];
      node_index[nid].llm_events.push(e);
    } else if (t === "tool_call" || t === "tool_result") {
      node_index[nid] = node_index[nid] || {};
      node_index[nid].tool_events = node_index[nid].tool_events || [];
      node_index[nid].tool_events.push(e);
    }
  }
  const sessStart = events.find(e => e.event_type === "session_start") || {};
  let totalTokens = 0, errorCount = 0;
  for (const e of events) {
    if (e.event_type === "llm_response") totalTokens += (e.total_tokens || 0);
    if (e.event_type === "node_error") errorCount++;
  }
  return {
    session_id: state.activeSessionId,
    agent_name: sessStart.agent_name || "live",
    input_query: sessStart.input_query || "",
    events, node_index, dag_edges,
    summary: {
      status: errorCount ? "error" : "running",
      node_count: Object.keys(node_index).length,
      error_count: errorCount,
      total_tokens: totalTokens,
      total_duration_ms: 0,
      error_nodes: events.filter(e => e.event_type === "node_error")
        .map(e => e.node_id),
    },
    _live: true,
  };
}

function renderCenter() {
  const root = $("#center");
  const bundle = getActiveBundleLike();
  if (!bundle) {
    root.innerHTML = `<div class="empty">Select a trace to inspect.</div>`;
    return;
  }
  const s = bundle.summary || {};
  const statusClass = s.status === "error" ? "err"
    : s.status === "success" ? "ok" : "";
  const statusLabel = s.status || "unknown";
  let html = `<h2>${escapeHtml(bundle.agent_name || "agent")}</h2>`;
  if (bundle.input_query) {
    html += `<div style="margin-bottom:14px;color:var(--muted);font-size:12px;">
      <strong>input:</strong> ${escapeHtml(bundle.input_query.slice(0, 280))}</div>`;
  }
  html += `<div class="summary-strip">
    <div class="stat"><div class="label">Status</div>
      <div class="value ${statusClass}">${statusLabel}</div></div>
    <div class="stat"><div class="label">Nodes</div>
      <div class="value">${s.node_count || 0}</div></div>
    <div class="stat"><div class="label">Errors</div>
      <div class="value ${(s.error_count||0)>0?'err':''}">${s.error_count || 0}</div></div>
    <div class="stat"><div class="label">Total tokens</div>
      <div class="value">${s.total_tokens || 0}</div></div>
    <div class="stat"><div class="label">Duration</div>
      <div class="value">${Math.round(s.total_duration_ms || 0)}ms</div></div>
  </div>`;
  html += `<h2>Execution graph</h2><div class="dag" id="dag"></div>`;
  html += `<h2 style="margin-top:18px;">Latency breakdown</h2>`;
  html += `<div class="latency-bars" id="latency"></div>`;
  if (state.replayResult) {
    html += renderReplayResult(state.replayResult);
  }
  root.innerHTML = html;
  renderDag(bundle);
  renderLatency(bundle);
}

function renderDag(bundle) {
  const root = $("#dag");
  if (!root) return;
  // Group nodes by depth for a simple top-down layout.
  const byDepth = {};
  const ordered = [];
  const seen = new Set();
  for (const ev of bundle.events) {
    if (ev.event_type !== "node_start") continue;
    if (!ev.node_id || seen.has(ev.node_id)) continue;
    seen.add(ev.node_id);
    ordered.push(ev);
    const d = ev.depth || 0;
    byDepth[d] = byDepth[d] || [];
    byDepth[d].push(ev);
  }
  if (!ordered.length) {
    root.innerHTML = `<div class="empty">Waiting for first event...</div>`;
    return;
  }
  const depths = Object.keys(byDepth).map(Number).sort((a, b) => a - b);
  let html = "";
  for (const d of depths) {
    html += `<div class="dag-row">`;
    for (const ev of byDepth[d]) {
      const ni = bundle.node_index[ev.node_id] || {};
      const end = ni.end_event;
      const err = ni.error_event;
      const status = err ? "error" : (end ? "ok" : "running");
      const dur = (end?.duration_ms || err?.duration_ms || 0);
      const selected = ev.node_id === state.activeNodeId ? " selected" : "";
      html += `<div class="node-card ${status}${selected}" data-id="${ev.node_id}">
        <div class="node-name">${escapeHtml(ev.node_name || "node")}</div>
        <div class="node-meta">
          <span class="type">${ev.node_type || ""}</span>
          <span>${dur ? Math.round(dur)+'ms' : '...'}</span>
        </div></div>`;
    }
    html += `</div>`;
  }
  root.innerHTML = html;
  $$(".node-card", root).forEach(el => {
    el.addEventListener("click", () => {
      state.activeNodeId = el.dataset.id;
      state.activeTab = "overview";
      renderCenter();
      renderDetail();
    });
  });
}

function renderLatency(bundle) {
  const root = $("#latency");
  if (!root) return;
  const rows = [];
  for (const [nid, ni] of Object.entries(bundle.node_index)) {
    const start = ni.start_event || {};
    const end = ni.end_event || {};
    const err = ni.error_event || {};
    const dur = end.duration_ms || err.duration_ms || 0;
    rows.push({
      nid, name: start.node_name || nid, dur, error: !!err,
    });
  }
  rows.sort((a, b) => b.dur - a.dur);
  const max = rows[0]?.dur || 1;
  if (!rows.length) {
    root.innerHTML = `<div style="color:var(--muted);font-size:11px;">no nodes yet</div>`;
    return;
  }
  root.innerHTML = rows.slice(0, 10).map(r => {
    const pct = Math.max(2, Math.round((r.dur / max) * 100));
    const color = r.error ? "var(--err)" : "var(--accent)";
    return `<div class="lb-row">
      <div class="lname">${escapeHtml(r.name)}</div>
      <div class="lbar"><div style="width:${pct}%;background:${color}"></div></div>
      <div class="lval">${Math.round(r.dur)}ms</div>
    </div>`;
  }).join("");
}
