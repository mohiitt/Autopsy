"""GeminiAgent - used when traces exceed ~32k tokens.

Uses Google's google-generativeai SDK. Always falls back to the heuristic
diagnosis if the API is unavailable.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from autopsy.core.events import DiagnosisResult, TraceBundle

from .gmi_agent import _extract_json, _heuristic_diagnosis
from .prompts import DIAGNOSIS_SYSTEM_PROMPT, build_diagnosis_user_prompt

logger = logging.getLogger("autopsy.diagnostics.gemini")


class GeminiAgent:
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 60.0,
    ):
        self.api_key = api_key or os.environ.get("GOOGLE_AI_API_KEY", "")
        self.model = model or os.environ.get("GEMINI_MODEL", "gemini-2.5-pro")
        self.timeout = timeout

    async def diagnose(
        self, bundle: TraceBundle, target_node_id: Optional[str] = None
    ) -> DiagnosisResult:
        heuristic = _heuristic_diagnosis(bundle, target_node_id)
        if not self.api_key:
            logger.warning("autopsy: GOOGLE_AI_API_KEY not set; using heuristic")
            return heuristic
        try:
            import google.generativeai as genai
            genai.configure(api_key=self.api_key)
            model = genai.GenerativeModel(
                model_name=self.model,
                system_instruction=DIAGNOSIS_SYSTEM_PROMPT,
            )
            user_prompt = build_diagnosis_user_prompt(bundle, target_node_id)
            # generate_content is sync; wrap in to_thread to avoid blocking.
            import asyncio
            resp = await asyncio.to_thread(
                model.generate_content, user_prompt,
                generation_config={"temperature": 0.2, "max_output_tokens": 900},
            )
            raw = ""
            try:
                raw = resp.text or ""
            except Exception:
                pass
            parsed = _extract_json(raw)
            if not parsed:
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
            logger.exception("autopsy: Gemini diagnose failed; using heuristic")
            return heuristic


def estimate_bundle_tokens(bundle: TraceBundle) -> int:
    """Rough size estimate (chars/4) of the full bundle as text."""
    import json
    try:
        return len(json.dumps(bundle.events, default=str)) // 4
    except Exception:
        return 0
