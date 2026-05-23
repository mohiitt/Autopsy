"""Diagnostics prompts and prompt builders."""
from __future__ import annotations

import json
from typing import Optional

from autopsy.core.events import TraceBundle


DIAGNOSIS_SYSTEM_PROMPT = """You are an expert AI agent debugger. You receive a trace of a multi-agent LLM pipeline that has failed or performed poorly, and you diagnose the root cause.

You MUST respond with a single valid JSON object matching this exact schema:
{
  "root_cause": "<1-2 sentences, plain English, no jargon>",
  "affected_node_id": "<node_id string>",
  "affected_node_name": "<human readable name>",
  "error_category": "<one of: context_overflow, bad_json, tool_failure, hallucination, timeout, prompt_issue, other>",
  "fix_suggestion": "<concrete actionable fix in plain English>",
  "fix_code_snippet": "<optional Python code snippet, or empty string>",
  "confidence": <0.0 to 1.0>,
  "latency_insight": "<optional: what is eating the most latency>",
  "estimated_latency_savings_ms": <number or 0>,
  "model_swap_suggestion": "<optional: suggest model swap at a specific node, or empty string>"
}

Rules:
- Be specific. Reference the actual node name and what it received.
- The fix_suggestion must be something the developer can implement in under 10 minutes.
- If the error is context_overflow, mention the specific character/token count.
- If you are unsure, lower confidence and say so in root_cause.
- Output ONLY the JSON object. No prose before or after, no markdown fences.
"""


def _truncate(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + "...[truncated]"


def build_diagnosis_user_prompt(bundle: TraceBundle, target_node_id: Optional[str]) -> str:
    """Build a detailed user message describing the failed trace.

    Stays under ~6000 tokens by aggressive truncation.
    """
    # Auto-detect target if not provided
    if not target_node_id:
        for ev in bundle.events:
            if ev.get("event_type") == "node_error":
                target_node_id = ev.get("node_id")
                break

    lines: list[str] = []
    lines.append("# Trace Summary")
    lines.append(f"Agent: {bundle.agent_name}")
    lines.append(f"Input: {_truncate(bundle.input_query, 500)}")
    summary = bundle.summary or {}
    lines.append(f"Status: {summary.get('status', 'unknown')}")
    lines.append(f"Total duration: {summary.get('total_duration_ms', 0):.0f}ms")
    lines.append(f"Total tokens: {summary.get('total_tokens', 0)}")
    lines.append(f"Node count: {summary.get('node_count', 0)}")
    lines.append(f"Error count: {summary.get('error_count', 0)}")
    lines.append("")

    # DAG of nodes
    lines.append("# Execution Graph (in order)")
    seen: set = set()
    for ev in bundle.events:
        if ev.get("event_type") != "node_start":
            continue
        nid = ev.get("node_id")
        if nid in seen:
            continue
        seen.add(nid)
        nidx = bundle.node_index.get(nid, {})
        end = nidx.get("end_event") or {}
        err = nidx.get("error_event") or {}
        status = "ERROR" if err else ("OK" if end else "RUNNING")
        dur = end.get("duration_ms") or err.get("duration_ms") or 0
        depth = ev.get("depth", 0)
        indent = "  " * int(depth)
        lines.append(
            f"{indent}- [{status}] {ev.get('node_name')} "
            f"(node_id={nid}, type={ev.get('node_type')}, dur={dur:.0f}ms)"
        )
    lines.append("")

    # Failed node details
    if target_node_id:
        lines.append(f"# Failed Node: {target_node_id}")
        nidx = bundle.node_index.get(target_node_id, {})
        start = nidx.get("start_event") or {}
        err = nidx.get("error_event") or {}
        end = nidx.get("end_event") or {}
        llm_events = nidx.get("llm_events") or []
        lines.append(f"Name: {start.get('node_name', '?')}")
        lines.append(f"Type: {start.get('node_type', '?')}")
        lines.append(f"Depth: {start.get('depth', 0)}")
        try:
            input_str = json.dumps(start.get("input_data"), default=str)
        except Exception:
            input_str = str(start.get("input_data"))
        lines.append(f"Input (first 1500 chars):\n{_truncate(input_str, 1500)}")
        if err:
            lines.append("")
            lines.append("## Error")
            lines.append(f"Type: {err.get('error_type', '')}")
            lines.append(f"Message: {_truncate(err.get('error_message', ''), 800)}")
            lines.append(f"Traceback (first 1500 chars):\n{_truncate(err.get('traceback', ''), 1500)}")
        if llm_events:
            lines.append("")
            lines.append("## LLM Calls at this node")
            for ev in llm_events[:4]:
                if ev.get("event_type") == "llm_request":
                    msgs = ev.get("messages", [])
                    try:
                        msg_str = json.dumps(msgs, default=str)
                    except Exception:
                        msg_str = str(msgs)
                    lines.append(f"- Request model={ev.get('model')} tokens_est={ev.get('prompt_tokens_estimate', 0)}")
                    lines.append(f"  messages: {_truncate(msg_str, 1200)}")
                elif ev.get("event_type") == "llm_response":
                    lines.append(
                        f"- Response tokens={ev.get('total_tokens', 0)} "
                        f"latency={ev.get('latency_ms', 0):.0f}ms "
                        f"finish={ev.get('finish_reason', '')}"
                    )
                    lines.append(f"  content: {_truncate(ev.get('content', ''), 800)}")

    lines.append("")
    lines.append("# Latency Breakdown (top 5 slowest nodes)")
    nodes_with_dur = []
    for nid, ni in bundle.node_index.items():
        end = ni.get("end_event") or {}
        err = ni.get("error_event") or {}
        d = end.get("duration_ms") or err.get("duration_ms") or 0
        name = (ni.get("start_event") or {}).get("node_name", nid)
        nodes_with_dur.append((d, name, nid))
    nodes_with_dur.sort(reverse=True)
    for d, name, nid in nodes_with_dur[:5]:
        lines.append(f"- {name} ({nid}): {d:.0f}ms")

    lines.append("")
    lines.append("Please diagnose the root cause and respond with the required JSON object only.")
    return "\n".join(lines)


LATENCY_ANALYSIS_PROMPT = """Analyze the latency breakdown and identify:
1. The single biggest latency bottleneck
2. Whether the bottleneck is avoidable
3. A specific recommendation to reduce latency by at least 20%

Respond in JSON only:
{"bottleneck_node": "...", "bottleneck_reason": "...", "recommendation": "...", "estimated_savings_ms": <number>}
"""
