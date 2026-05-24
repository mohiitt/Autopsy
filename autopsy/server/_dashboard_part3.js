function renderDetail() {
  const root = $("#detail");
  const bundle = getActiveBundleLike();
  if (!bundle || !state.activeNodeId) {
    root.innerHTML = `<div class="empty">Click a node to see details.</div>`;
    return;
  }
  const nid = state.activeNodeId;
  const ni = bundle.node_index[nid] || {};
  const start = ni.start_event || {};
  const endEv = ni.end_event || null;
  const errEv = ni.error_event || null;
  const end = endEv || {};
  const err = errEv || {};
  const llmEvs = ni.llm_events || [];
  const status = errEv ? "error" : (endEv ? "ok" : "running");
  const dur = end.duration_ms || err.duration_ms || 0;

  const tabs = [
    ["overview", "Overview"],
    ["io", "Input / Output"],
    ["llm", `LLM (${llmEvs.length})`],
    ["diag", "Diagnose" + (state.diagnosis ? " ✓" : "")],
  ];
  let html = `<h3>${escapeHtml(start.node_name || "node")}</h3>
    <div class="sub">id: <code>${escapeHtml(nid)}</code> · depth ${start.depth || 0}
      · ${status === "error" ? "❌ error" : status === "ok" ? "✅ ok" : "⏳ running"}</div>
    <div class="tab-row">
      ${tabs.map(([k, l]) =>
        `<div class="tab${state.activeTab === k ? " active" : ""}"
          data-tab="${k}">${l}</div>`).join("")}
    </div>`;

  if (state.activeTab === "overview") {
    html += `<div class="kv">
      <div class="k">Type</div><div>${escapeHtml(start.node_type || "")}</div>
      <div class="k">Duration</div><div>${Math.round(dur)}ms</div>
      <div class="k">Parent</div><div>${escapeHtml(start.parent_node_id || "(root)")}</div>
      <div class="k">Status</div><div>${status}</div>
    </div>`;
    if (errEv) {
      html += `<h3 style="color:var(--err);">Error</h3>
        <div class="kv">
          <div class="k">Type</div><div>${escapeHtml(err.error_type || "")}</div>
          <div class="k">Message</div><div>${escapeHtml(err.error_message || "")}</div>
        </div>
        <pre class="code err">${escapeHtml((err.traceback || "").slice(0, 2000))}</pre>`;
    }
    html += `<div class="btn-row">
      <button class="btn" id="btn-diagnose">🔍 Diagnose this node</button>
      <button class="btn ghost" id="btn-replay">↻ Replay from here</button>
    </div>`;
    if (state.diagnosis) html += renderDiagnosis(state.diagnosis);
  } else if (state.activeTab === "io") {
    html += `<h3>Input</h3>
      <pre class="code">${escapeHtml(safeJSON(start.input_data))}</pre>`;
    if (endEv) {
      html += `<h3 style="margin-top:14px;">Output</h3>
        <pre class="code">${escapeHtml(safeJSON(end.output_data))}</pre>`;
    }
  } else if (state.activeTab === "llm") {
    if (!llmEvs.length) {
      html += `<div class="empty">No LLM calls at this node.</div>`;
    } else {
      for (const ev of llmEvs) {
        if (ev.event_type === "llm_request") {
          html += `<div style="margin-bottom:10px;">
            <span class="badge">request</span>
            <span class="badge">${escapeHtml(ev.model || "")}</span>
            <span class="badge">≈${ev.prompt_tokens_estimate || 0} tok</span>
            <pre class="code" style="margin-top:6px;">${escapeHtml(safeJSON(ev.messages))}</pre>
          </div>`;
        } else if (ev.event_type === "llm_response") {
          html += `<div style="margin-bottom:10px;">
            <span class="badge">response</span>
            <span class="badge">${escapeHtml(ev.finish_reason || "")}</span>
            <span class="badge">${ev.total_tokens || 0} tok</span>
            <span class="badge">${Math.round(ev.latency_ms || 0)}ms</span>
            <pre class="code" style="margin-top:6px;">${escapeHtml(ev.content || "")}</pre>
          </div>`;
        }
      }
    }
  } else if (state.activeTab === "diag") {
    if (state.diagnosisLoading) {
      html += `<div class="empty">🔍 Diagnosing... GMI Cloud is thinking.</div>`;
    } else if (state.diagnosis) {
      html += renderDiagnosis(state.diagnosis);
    } else {
      html += `<div class="empty">No diagnosis yet.<br/><br/>
        <button class="btn" id="btn-diagnose-2">Run diagnosis</button></div>`;
    }
  }
  root.innerHTML = html;
  $$(".tab", root).forEach(el => {
    el.addEventListener("click", () => {
      state.activeTab = el.dataset.tab;
      renderDetail();
    });
  });
  const bDiag = $("#btn-diagnose", root) || $("#btn-diagnose-2", root);
  if (bDiag) bDiag.addEventListener("click", () => runDiagnose(nid));
  const bReplay = $("#btn-replay", root);
  if (bReplay) bReplay.addEventListener("click", () => runReplay(nid));
}

function safeJSON(v) {
  if (v == null) return "null";
  try { return JSON.stringify(v, null, 2); }
  catch (e) { return String(v); }
}

function renderDiagnosis(d) {
  const confPct = Math.round((d.confidence || 0) * 100);
  const rr = d.rocketride || null;
  const rrBanner = (rr && rr.used) ? `
    <div class="rr-banner">
      <span class="dot-rr" style="display:inline-block;width:6px;height:6px;
        border-radius:50%;background:var(--ok);margin-right:6px;"></span>
      Trace pre-processed by RocketRide pipeline
      <code>${escapeHtml(rr.pipeline || "autopsy_diagnose.pipe")}</code>
      ${rr.similar_cases > 0
        ? ` — found <strong>${rr.similar_cases}</strong> similar past failure(s)`
        : ""}
    </div>` : "";
  return `<div class="diag-card">
    ${rrBanner}
    <h4>🔍 Root cause</h4>
    <div style="margin-bottom:10px;">${escapeHtml(d.root_cause || "")}</div>
    <div class="kv">
      <div class="k">Node</div><div>${escapeHtml(d.affected_node_name || "")}</div>
      <div class="k">Category</div><div><span class="badge">${escapeHtml(d.error_category || "")}</span></div>
      <div class="k">Confidence</div><div>
        <div class="bar"><div style="width:${confPct}%"></div></div>
        <span style="font-size:11px;color:var(--muted);">${confPct}%</span>
      </div>
    </div>
    <h4 style="margin-top:8px;">💡 Fix</h4>
    <div style="margin-bottom:8px;">${escapeHtml(d.fix_suggestion || "")}</div>
    ${d.fix_code_snippet ? `<pre class="code">${escapeHtml(d.fix_code_snippet)}</pre>` : ""}
    ${d.latency_insight ? `<h4 style="margin-top:8px;">⚡ Latency</h4>
      <div>${escapeHtml(d.latency_insight)}</div>
      ${d.estimated_latency_savings_ms ?
        `<div style="color:var(--muted);font-size:11px;">est. savings: ${Math.round(d.estimated_latency_savings_ms)}ms</div>` : ""}` : ""}
    <div class="btn-row" style="margin-top:12px;">
      <button class="btn" id="btn-apply-replay">↻ Apply fix &amp; replay</button>
    </div>
  </div>`;
}

function renderReplayResult(r) {
  const comp = r.comparison || {};
  const o = comp.original || {};
  const re = comp.replay || {};
  return `<h2 style="margin-top:18px;">Replay vs original</h2>
  <div class="diag-card">
    <h4>↻ Replay complete</h4>
    <div style="margin-bottom:8px;">${escapeHtml(r.fix_description || "")}</div>
    <table style="width:100%;font-size:12px;border-collapse:collapse;">
      <thead><tr style="color:var(--muted);text-align:left;">
        <th></th><th>Original</th><th>Replay</th><th>Δ</th></tr></thead>
      <tbody>
        <tr><td>Status</td>
          <td style="color:${o.status === 'error' ? 'var(--err)' : 'var(--ok)'};">${o.status}</td>
          <td style="color:var(--ok);">${re.status}</td>
          <td>—</td></tr>
        <tr><td>Errors</td>
          <td>${o.errors || 0}</td><td>${re.errors || 0}</td>
          <td>−${(o.errors || 0) - (re.errors || 0)}</td></tr>
        <tr><td>Duration</td>
          <td>${Math.round(o.duration_ms || 0)}ms</td>
          <td>${Math.round(re.duration_ms || 0)}ms</td>
          <td style="color:var(--ok);">${Math.round(comp.latency_delta_ms || 0)}ms</td></tr>
        <tr><td>Tokens</td>
          <td>${o.tokens || 0}</td><td>${re.tokens || 0}</td>
          <td>${comp.token_delta || 0}</td></tr>
      </tbody>
    </table>
    <div style="margin-top:10px;color:var(--muted);font-size:11px;">${escapeHtml(r.side_effect_warning || "")}</div>
  </div>`;
}

async function runDiagnose(nodeId) {
  if (state.diagnosisLoading) return;
  state.diagnosisLoading = true;
  state.activeTab = "diag";
  renderDetail();
  try {
    const r = await fetch(
      `/api/sessions/${state.activeSessionId}/diagnose`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ node_id: nodeId }),
      });
    if (!r.ok) throw new Error(`status ${r.status}`);
    state.diagnosis = await r.json();
    toast("Diagnosis complete");
  } catch (e) {
    toast(`Diagnose failed: ${e.message}`, "err");
  } finally {
    state.diagnosisLoading = false;
    renderDetail();
  }
}

async function runReplay(nodeId) {
  try {
    const fix = state.diagnosis?.fix_suggestion?.slice(0, 200)
      || "Applied developer fix";
    const r = await fetch(
      `/api/sessions/${state.activeSessionId}/replay`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ node_id: nodeId, fix_description: fix }),
      });
    if (!r.ok) throw new Error(`status ${r.status}`);
    state.replayResult = await r.json();
    // Also signal the live demo pipeline so the next loop iteration succeeds.
    try {
      const r2 = await fetch("/api/demo/fix", { method: "POST" });
      if (r2.ok) {
        state.demoFixed = true;
        toast("✅ Fix applied — next iteration of the live pipeline will go green");
      } else {
        toast("Replay complete - errors fixed!");
      }
    } catch (_) {
      toast("Replay complete - errors fixed!");
    }
    renderCenter();
    renderHeaderStatus();
  } catch (e) {
    toast(`Replay failed: ${e.message}`, "err");
  }
}

async function demoReset() {
  try {
    const r = await fetch("/api/demo/reset", { method: "POST" });
    if (r.ok) {
      state.demoFixed = false;
      toast("Demo reset — pipeline will start failing again");
      renderHeaderStatus();
    }
  } catch (e) {
    toast(`Reset failed: ${e.message}`, "err");
  }
}

async function fetchDemoStatus() {
  try {
    const r = await fetch("/api/demo/status");
    if (r.ok) {
      const d = await r.json();
      state.demoFixed = !!d.fix_applied;
      renderHeaderStatus();
    }
  } catch (_) { /* ignore */ }
}

async function fetchRocketRideStatus() {
  const el = document.getElementById("rr-status");
  if (!el) return;
  try {
    const r = await fetch("/api/rocketride/status");
    if (!r.ok) throw new Error("status " + r.status);
    const d = await r.json();
    const cls = d.ok ? "ok" : "off";
    const label = d.ok ? "RocketRide: connected"
      : (d.engine_reachable ? "RocketRide: ready"
         : (d.sdk_installed ? "RocketRide: engine offline"
            : "RocketRide: not installed"));
    el.className = "rr-status " + cls;
    el.innerHTML = '<span class="dot-rr"></span>' + label;
    el.title = (d.detail || "") + "\npipe: " + (d.pipe_path || "?") +
               "\nuri: " + (d.uri || "?");
  } catch (e) {
    el.className = "rr-status off";
    el.innerHTML = '<span class="dot-rr"></span>RocketRide: offline';
    el.title = String(e);
  }
}

function renderHeaderStatus() {
  const el = document.getElementById("demo-status");
  if (!el) return;
  if (state.demoFixed) {
    el.innerHTML = `<span class="pill ok-pill">demo: fixed</span>
      <button class="btn ghost btn-sm" id="btn-demo-reset">↺ reset</button>`;
  } else {
    el.innerHTML = `<span class="pill err-pill">demo: failing</span>`;
  }
}

document.addEventListener("click", (e) => {
  if (e.target.id === "btn-apply-replay" && state.activeNodeId) {
    runReplay(state.activeNodeId);
  }
  if (e.target.id === "btn-demo-reset") {
    demoReset();
  }
});

fetchSessions();
fetchDemoStatus();
fetchRocketRideStatus();
setInterval(fetchRocketRideStatus, 30000);
connectWS();
