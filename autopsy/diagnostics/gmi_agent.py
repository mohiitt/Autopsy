"""GMIAgent - diagnoses traces using GMI Cloud (OpenAI-compatible API).

Designed for the hackathon: fast (<2s typical), strong reasoning, free tier.

Defensive design:
- If the API call fails (network, auth), returns a sensible heuristic
  DiagnosisResult derived locally from the bundle so the demo never breaks.
- If the LLM returns invalid JSON, attempts repair, then falls back to heuristics.
- Always returns a DiagnosisResult; never raises.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

from autopsy.core.events import DiagnosisResult, TraceBundle
from autopsy.core.interceptor import restore_tracing, suppress_tracing

from .prompts import DIAGNOSIS_SYSTEM_PROMPT, build_diagnosis_user_prompt

logger = logging.getLogger("autopsy.diagnostics.gmi")


def _heuristic_diagnosis(bundle: TraceBundle, target_node_id: Optional[str]) -> DiagnosisResult:
    """Local fallback: produce a useful diagnosis from the bundle alone."""
    # Find first error
    if not target_node_id:
        for ev in bundle.events:
            if ev.get("event_type") == "node_error":
                target_node_id = ev.get("node_id")
                break
    nidx = bundle.node_index.get(target_node_id or "", {})
    start = nidx.get("start_event") or {}
    err = nidx.get("error_event") or {}
    err_msg = (err.get("error_message") or "").lower()
    err_type = err.get("error_type", "")
    node_name = start.get("node_name", "unknown")

    if "json" in err_msg or err_type == "JSONDecodeError" or "decode" in err_msg:
        category = "bad_json"
        cause = (
            f"The {node_name} node received malformed JSON, likely because the upstream "
            f"context exceeded the model's effective limit and the model truncated its output."
        )
        fix = (
            "Chunk the input before sending it to the summarizer. Split the search results "
            "into smaller pieces (~1500 chars each), summarize each chunk separately, then "
            "combine the chunk summaries."
        )
        snippet = (
            "async def summarizer_agent(context: str) -> dict:\n"
            "    CHUNK_SIZE = 1500\n"
            "    chunks = [context[i:i + CHUNK_SIZE] "
            "for i in range(0, len(context), CHUNK_SIZE)]\n"
            "    summaries = [await summarize_chunk(c) for c in chunks]\n"
            "    return {'summary': ' '.join(summaries), 'key_points': []}"
        )
    elif "context" in err_msg or "overflow" in err_msg or "too long" in err_msg:
        category = "context_overflow"
        cause = (
            f"The {node_name} node was given a context that exceeds the model's safe input window."
        )
        fix = (
            "Add a truncation step or summarization pre-pass before this node. "
            "Cap inputs to 8k chars and chunk anything larger."
        )
        snippet = (
            "MAX_CONTEXT = 8000\n"
            "context = context[:MAX_CONTEXT]"
        )
    elif "timeout" in err_msg or err_type in ("TimeoutError", "asyncio.TimeoutError"):
        category = "timeout"
        cause = f"The {node_name} node timed out."
        fix = "Increase the request timeout or break work into smaller calls."
        snippet = ""
    elif err_type:
        category = "tool_failure"
        cause = f"The {node_name} node raised {err_type}: {err.get('error_message', '')[:200]}"
        fix = "Add input validation and a retry-with-backoff wrapper at this node."
        snippet = ""
    else:
        category = "other"
        cause = "No obvious error detected; the trace may have a quality issue rather than a failure."
        fix = "Run replay with a different model to compare outputs."
        snippet = ""

    return DiagnosisResult(
        root_cause=cause,
        affected_node_id=target_node_id or "",
        affected_node_name=node_name,
        error_category=category,
        fix_suggestion=fix,
        fix_code_snippet=snippet,
        confidence=0.6,
        latency_insight="",
        estimated_latency_savings_ms=0.0,
        model_swap_suggestion="",
        raw_response="(local heuristic - LLM unavailable)",
    )


def _extract_json(text: str) -> Optional[dict]:
    """Try several strategies to extract a JSON object from an LLM response."""
    if not text:
        return None
    # Strip markdown fences
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    # Try direct parse
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    # Try to find the first {...} block
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            pass
    return None


class GMIAgent:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        fallback_model: Optional[str] = None,
        timeout: float = 10.0,
    ):
        self.api_key = api_key or os.environ.get("GMI_API_KEY", "")
        self.base_url = base_url or os.environ.get(
            "GMI_BASE_URL", "https://api.gmi-serving.com/v1")
        self.model = model or os.environ.get(
            "GMI_DEFAULT_MODEL", "deepseek-ai/DeepSeek-V3.2")
        self.fallback_model = fallback_model or os.environ.get(
            "GMI_FALLBACK_MODEL", "Qwen/Qwen3-Next-80B-A3B-Instruct")
        self.timeout = timeout

    async def diagnose(
        self, bundle: TraceBundle, target_node_id: Optional[str] = None
    ) -> DiagnosisResult:
        # Always have a fallback ready.
        heuristic = _heuristic_diagnosis(bundle, target_node_id)
        if not self.api_key:
            logger.warning("autopsy: GMI_API_KEY not set; using heuristic diagnosis")
            return heuristic

        user_prompt = build_diagnosis_user_prompt(bundle, target_node_id)

        # Suppress tracing so diagnostics calls don't show in the user's session.
        token = suppress_tracing()
        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(
                api_key=self.api_key, base_url=self.base_url, timeout=self.timeout)
            try:
                completion = await client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": DIAGNOSIS_SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.2,
                    max_tokens=600,
                )
            except Exception as exc:
                logger.warning("GMI primary model failed (%s), trying fallback", exc)
                try:
                    completion = await client.chat.completions.create(
                        model=self.fallback_model,
                        messages=[
                            {"role": "system", "content": DIAGNOSIS_SYSTEM_PROMPT},
                            {"role": "user", "content": user_prompt},
                        ],
                        temperature=0.2,
                        max_tokens=600,
                    )
                except Exception as exc2:
                    logger.warning("GMI fallback model failed (%s); using heuristic", exc2)
                    heuristic.raw_response = f"GMI API failed: {exc2}"
                    return heuristic

            raw = ""
            try:
                raw = completion.choices[0].message.content or ""
            except Exception:
                pass

            parsed = _extract_json(raw)
            if not parsed:
                logger.warning("GMI returned non-JSON response; using heuristic")
                heuristic.raw_response = raw[:2000]
                return heuristic

            return DiagnosisResult(
                root_cause=str(parsed.get("root_cause", heuristic.root_cause))[:1500],
                affected_node_id=str(parsed.get(
                    "affected_node_id", heuristic.affected_node_id)),
                affected_node_name=str(parsed.get(
                    "affected_node_name", heuristic.affected_node_name)),
                error_category=str(parsed.get(
                    "error_category", heuristic.error_category)),
                fix_suggestion=str(parsed.get(
                    "fix_suggestion", heuristic.fix_suggestion))[:2000],
                fix_code_snippet=str(parsed.get("fix_code_snippet", ""))[:3000],
                confidence=float(parsed.get("confidence", 0.7) or 0.7),
                latency_insight=str(parsed.get("latency_insight", ""))[:1000],
                estimated_latency_savings_ms=float(
                    parsed.get("estimated_latency_savings_ms", 0) or 0),
                model_swap_suggestion=str(
                    parsed.get("model_swap_suggestion", ""))[:500],
                raw_response=raw[:4000],
            )
        except Exception:
            logger.exception("autopsy: GMI diagnose failed; using heuristic")
            return heuristic
        finally:
            restore_tracing(token)
