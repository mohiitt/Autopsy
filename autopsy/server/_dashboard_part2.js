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

  // Collect node_start events, deduped by node_id, preserving insertion order.
  const nodes = [];
  const seen = new Set();
  for (const ev of bundle.events) {
    if (ev.event_type !== "node_start") continue;
    if (!ev.node_id || seen.has(ev.node_id)) continue;
    seen.add(ev.node_id);
    nodes.push(ev);
  }
  if (!nodes.length) {
    root.innerHTML = '<div class="empty">Waiting for first event...</div>';
    return;
  }

  // Index nodes and build parent->children map.
  const nodeMeta = {};
  for (const ev of nodes) {
    nodeMeta[ev.node_id] = {
      ev,
      depth: ev.depth || 0,
      parentId: ev.parent_node_id || null,
      children: [],
    };
  }
  const roots = [];
  for (const m of Object.values(nodeMeta)) {
    if (m.parentId && nodeMeta[m.parentId]) {
      nodeMeta[m.parentId].children.push(m);
    } else {
      roots.push(m);
    }
  }

  // Tidy-tree-ish layout: assign each subtree a horizontal "slot count" equal
  // to the number of leaves, then place leaves left-to-right at their depth,
  // and place parents centered above their children.
  const SLOT_W = 196;     // horizontal distance between adjacent leaves
  const ROW_H = 112;      // vertical distance between depth levels
  const NODE_W = 168;
  const NODE_H = 60;
  const PAD = 28;

  let nextSlot = 0;
  const positions = {};

  function layout(meta) {
    if (meta.children.length === 0) {
      const slot = nextSlot++;
      positions[meta.ev.node_id] = {
        slot,
        x: PAD + slot * SLOT_W,
        y: PAD + meta.depth * ROW_H,
      };
      return slot;
    }
    const childSlots = meta.children.map(layout);
    const minS = Math.min(...childSlots);
    const maxS = Math.max(...childSlots);
    const centerSlot = (minS + maxS) / 2;
    positions[meta.ev.node_id] = {
      slot: centerSlot,
      x: PAD + centerSlot * SLOT_W,
      y: PAD + meta.depth * ROW_H,
    };
    return centerSlot;
  }
  roots.forEach(layout);

  // Compute canvas size.
  let maxX = 0, maxY = 0;
  for (const p of Object.values(positions)) {
    if (p.x > maxX) maxX = p.x;
    if (p.y > maxY) maxY = p.y;
  }
  const canvasW = maxX + NODE_W + PAD;
  const canvasH = maxY + NODE_H + PAD;

  // Build SVG edges (drawn first so they're behind the cards).
  const edgeMarkup = [];
  for (const meta of Object.values(nodeMeta)) {
    const parent = positions[meta.ev.node_id];
    if (!parent) continue;
    for (const child of meta.children) {
      const c = positions[child.ev.node_id];
      if (!c) continue;
      const x1 = parent.x + NODE_W / 2;
      const y1 = parent.y + NODE_H;
      const x2 = c.x + NODE_W / 2;
      const y2 = c.y;
      const midY = (y1 + y2) / 2;
      const path = "M " + x1 + " " + y1 + " C " + x1 + " " + midY + ", " + x2 + " " + midY + ", " + x2 + " " + y2;
      const ni = bundle.node_index[child.ev.node_id] || {};
      const errChild = !!ni.error_event;
      const okChild = !!ni.end_event;
      const stroke = errChild ? "#ff5d5d" : (okChild ? "#3ddc97" : "#ffb84d");
      const markerId = errChild ? "arrowhead-err" : (okChild ? "arrowhead-ok" : "arrowhead");
      const isRunning = !errChild && !okChild;
      edgeMarkup.push(
        '<path d="' + path + '" fill="none" stroke="' + stroke + '" stroke-width="2.5" opacity="0.95" ' +
        (isRunning ? 'stroke-dasharray="6 4" class="edge-animated" ' : '') +
        'marker-end="url(#' + markerId + ')" />'
      );
    }
  }

  // Build node HTML (absolute positioned).
  const nodeHtml = nodes.map(ev => {
    const pos = positions[ev.node_id];
    if (!pos) return "";
    const ni = bundle.node_index[ev.node_id] || {};
    const end = ni.end_event;
    const err = ni.error_event;
    const status = err ? "error" : (end ? "ok" : "running");
    const dur = (end && end.duration_ms) || (err && err.duration_ms) || 0;
    const selected = ev.node_id === state.activeNodeId ? " selected" : "";
    const durLabel = dur ? Math.round(dur) + "ms" : "running...";
    return '<div class="node-card ' + status + selected + '" data-id="' + ev.node_id + '" ' +
      'style="position:absolute;left:' + pos.x + 'px;top:' + pos.y + 'px;width:' + NODE_W + 'px;height:' + NODE_H + 'px;">' +
      '<div class="node-name">' + escapeHtml(ev.node_name || "node") + '</div>' +
      '<div class="node-meta">' +
        '<span class="type">' + (ev.node_type || "") + '</span>' +
        '<span>' + durLabel + '</span>' +
      '</div></div>';
  }).join("");

  const svg =
    '<svg width="' + canvasW + '" height="' + canvasH + '" ' +
      'style="position:absolute;left:0;top:0;pointer-events:none;overflow:visible;z-index:1;">' +
      '<defs>' +
        '<marker id="arrowhead" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">' +
          '<path d="M 0 0 L 10 5 L 0 10 z" fill="#ffb84d" />' +
        '</marker>' +
        '<marker id="arrowhead-err" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">' +
          '<path d="M 0 0 L 10 5 L 0 10 z" fill="#ff5d5d" />' +
        '</marker>' +
        '<marker id="arrowhead-ok" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">' +
          '<path d="M 0 0 L 10 5 L 0 10 z" fill="#3ddc97" />' +
        '</marker>' +
      '</defs>' +
      edgeMarkup.join("") +
    '</svg>';

  // Auto-fit: scale the canvas down if it's wider than the available viewport.
  const viewportW = (root.clientWidth || 800) - 16;
  const autoScale = canvasW > viewportW
    ? Math.max(0.35, viewportW / canvasW) : 1;
  const initX = 0, initY = 0;

  root.innerHTML =
    '<div class="dag-viewport" id="dag-viewport">' +
      '<div class="dag-controls">' +
        '<button class="dag-btn" data-act="zoom-in" title="Zoom in">+</button>' +
        '<button class="dag-btn" data-act="zoom-out" title="Zoom out">−</button>' +
        '<button class="dag-btn" data-act="zoom-reset" title="Fit to view">⊡</button>' +
        '<span class="dag-zoom-label" id="dag-zoom-label">' + Math.round(autoScale * 100) + '%</span>' +
      '</div>' +
      '<div class="dag-canvas" id="dag-canvas" ' +
           'style="width:' + canvasW + 'px;height:' + canvasH + 'px;' +
           'transform: translate(' + initX + 'px, ' + initY + 'px) scale(' + autoScale + ');' +
           'transform-origin: 0 0;">' +
        svg + nodeHtml +
      '</div>' +
    '</div>';

  setupPanZoom(root.querySelector("#dag-viewport"),
               root.querySelector("#dag-canvas"),
               root.querySelector("#dag-zoom-label"),
               { x: initX, y: initY, scale: autoScale });

  $$(".node-card", root).forEach(el => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      state.activeNodeId = el.dataset.id;
      state.activeTab = "overview";
      renderCenter();
      renderDetail();
    });
  });
}

function setupPanZoom(viewport, canvas, label, init) {
  if (!viewport || !canvas) return;
  const view = { x: init.x, y: init.y, scale: init.scale };

  function apply() {
    canvas.style.transform =
      "translate(" + view.x + "px, " + view.y + "px) scale(" + view.scale + ")";
    if (label) label.textContent = Math.round(view.scale * 100) + "%";
  }
  function setScale(next, cx, cy) {
    // Zoom around the cursor point (cx,cy) so it stays put.
    const rect = viewport.getBoundingClientRect();
    const px = (cx ?? rect.width / 2) - rect.left;
    const py = (cy ?? rect.height / 2) - rect.top;
    const wx = (px - view.x) / view.scale;
    const wy = (py - view.y) / view.scale;
    view.scale = Math.max(0.2, Math.min(2.5, next));
    view.x = px - wx * view.scale;
    view.y = py - wy * view.scale;
    apply();
  }

  // Drag-to-pan (left mouse OR space-not-required: middle works too).
  let dragging = false, lastX = 0, lastY = 0;
  viewport.addEventListener("mousedown", (e) => {
    if (e.target.closest(".node-card") || e.target.closest(".dag-btn")) return;
    dragging = true; lastX = e.clientX; lastY = e.clientY;
    viewport.style.cursor = "grabbing";
    e.preventDefault();
  });
  window.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    view.x += e.clientX - lastX;
    view.y += e.clientY - lastY;
    lastX = e.clientX; lastY = e.clientY;
    apply();
  });
  window.addEventListener("mouseup", () => {
    dragging = false; viewport.style.cursor = "";
  });
  // Wheel-to-zoom (ctrl/cmd OR plain wheel).
  viewport.addEventListener("wheel", (e) => {
    e.preventDefault();
    const factor = e.deltaY < 0 ? 1.1 : 1 / 1.1;
    setScale(view.scale * factor, e.clientX, e.clientY);
  }, { passive: false });
  // Controls.
  viewport.querySelectorAll(".dag-btn").forEach(btn => {
    btn.addEventListener("click", (e) => {
      const act = btn.dataset.act;
      if (act === "zoom-in") setScale(view.scale * 1.2);
      else if (act === "zoom-out") setScale(view.scale / 1.2);
      else if (act === "zoom-reset") {
        view.x = init.x; view.y = init.y; view.scale = init.scale; apply();
      }
      e.stopPropagation();
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
