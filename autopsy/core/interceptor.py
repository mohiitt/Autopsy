"""LLM call interceptor - monkey-patches OpenAI SDK to record LLM events.

Strategy:
- We patch openai.resources.chat.completions.AsyncCompletions.create.
- The patched create() emits LLMRequestEvent before the call and LLMResponseEvent
  after.
- The patch uses contextvars to determine the active TraceSession - if none, it
  passes through unchanged. This means it is safe to import the patched module
  even when autopsy is not actively tracing.
- For non-OpenAI providers (LangChain, raw httpx), the same OpenAI patch covers
  them when they use the OpenAI Python SDK with a custom base_url (the most
  common case for GMI, Together, etc.).
- A context variable _in_diagnostics_call suppresses tracing for nested
  diagnostics LLM calls so they don't pollute the user's session.

The interceptor is intentionally defensive - if any part fails, the original
function is still invoked so the user's agent always works.
"""
from __future__ import annotations

import contextvars
import json
import logging
import time
from typing import Any

from .events import LLMRequestEvent, LLMResponseEvent, ToolCallEvent
from .tracer import get_current_session

logger = logging.getLogger("autopsy.interceptor")

_in_diagnostics_call: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_in_diagnostics_call", default=False
)


def suppress_tracing() -> contextvars.Token:
    """Set the flag to suppress tracing - returns token to reset."""
    return _in_diagnostics_call.set(True)


def restore_tracing(token: contextvars.Token) -> None:
    try:
        _in_diagnostics_call.reset(token)
    except Exception:
        pass


def _estimate_tokens(messages: list[dict], model: str) -> int:
    """Approximate prompt tokens. Falls back to char/4 for non-OpenAI models."""
    try:
        import tiktoken
        try:
            enc = tiktoken.encoding_for_model(model)
        except Exception:
            enc = tiktoken.get_encoding("cl100k_base")
        total = 0
        for m in messages or []:
            c = m.get("content")
            if isinstance(c, str):
                total += len(enc.encode(c))
            elif isinstance(c, list):
                for part in c:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        total += len(enc.encode(part["text"]))
        return total
    except Exception:
        try:
            total_chars = 0
            for m in messages or []:
                c = m.get("content")
                if isinstance(c, str):
                    total_chars += len(c)
                elif isinstance(c, list):
                    for part in c:
                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                            total_chars += len(part["text"])
            return total_chars // 4
        except Exception:
            return 0


def _safe_dump(obj: Any) -> Any:
    """Convert SDK objects (pydantic models) to JSON-safe dicts."""
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: _safe_dump(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_safe_dump(v) for v in obj]
    for attr in ("model_dump", "dict", "to_dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return _safe_dump(fn())
            except Exception:
                continue
    try:
        return str(obj)
    except Exception:
        return "<unserializable>"


class _PatchedCreate:
    """Wraps the original AsyncCompletions.create method."""

    def __init__(self, original):
        self._original = original

    async def __call__(self, *args, **kwargs):
        # Pass-through if tracing is suppressed or there's no active session.
        if _in_diagnostics_call.get():
            return await self._original(*args, **kwargs)
        session = get_current_session()
        if session is None:
            return await self._original(*args, **kwargs)

        from .decorator import current_node_id

        node_id = current_node_id() or "root"
        model = kwargs.get("model", "")
        messages = kwargs.get("messages", []) or []
        tools = kwargs.get("tools", []) or []
        temperature = kwargs.get("temperature", 1.0)
        max_tokens = kwargs.get("max_tokens", 0) or 0
        stream = kwargs.get("stream", False)

        prompt_estimate = _estimate_tokens(messages, model)

        req_ev = LLMRequestEvent(
            session_id=session.session_id,
            node_id=node_id,
            model=str(model),
            messages=_safe_dump(messages) or [],
            temperature=float(temperature) if temperature is not None else 1.0,
            max_tokens=int(max_tokens) if max_tokens else 0,
            tools=_safe_dump(tools) or [],
            prompt_tokens_estimate=int(prompt_estimate),
        )
        try:
            await session.emit(req_ev)
        except Exception:
            logger.exception("autopsy: emit llm_request failed")

        t0 = time.perf_counter()
        try:
            result = await self._original(*args, **kwargs)
        except Exception as exc:
            # Emit a failed response so the dashboard shows what happened.
            try:
                resp_ev = LLMResponseEvent(
                    session_id=session.session_id,
                    node_id=node_id,
                    model=str(model),
                    content="",
                    tool_calls=[],
                    prompt_tokens=0,
                    completion_tokens=0,
                    total_tokens=0,
                    latency_ms=(time.perf_counter() - t0) * 1000,
                    finish_reason=f"error:{type(exc).__name__}",
                )
                await session.emit(resp_ev)
            except Exception:
                pass
            raise

        # Streaming path: collect chunks then emit one synthetic response.
        if stream:
            return self._wrap_stream(result, session, node_id, model, t0)

        # Non-streaming path.
        try:
            content = ""
            tool_calls_list: list[dict] = []
            usage = None
            finish_reason = ""
            choices = getattr(result, "choices", None) or []
            if choices:
                msg = getattr(choices[0], "message", None)
                content = getattr(msg, "content", "") or ""
                raw_tools = getattr(msg, "tool_calls", None) or []
                for tc in raw_tools:
                    tool_calls_list.append({
                        "id": getattr(tc, "id", ""),
                        "type": getattr(tc, "type", "function"),
                        "name": getattr(getattr(tc, "function", None), "name", ""),
                        "arguments": getattr(
                            getattr(tc, "function", None), "arguments", ""),
                    })
                finish_reason = getattr(choices[0], "finish_reason", "") or ""
            usage_obj = getattr(result, "usage", None)
            if usage_obj is not None:
                usage = {
                    "prompt_tokens": getattr(usage_obj, "prompt_tokens", 0) or 0,
                    "completion_tokens": getattr(usage_obj, "completion_tokens", 0) or 0,
                    "total_tokens": getattr(usage_obj, "total_tokens", 0) or 0,
                }
            resp_ev = LLMResponseEvent(
                session_id=session.session_id,
                node_id=node_id,
                model=str(model),
                content=content or "",
                tool_calls=tool_calls_list,
                prompt_tokens=(usage or {}).get("prompt_tokens", 0),
                completion_tokens=(usage or {}).get("completion_tokens", 0),
                total_tokens=(usage or {}).get("total_tokens", 0),
                latency_ms=(time.perf_counter() - t0) * 1000,
                finish_reason=finish_reason,
            )
            await session.emit(resp_ev)

            # Also emit tool_call events for any tools the model invoked.
            for tc in tool_calls_list:
                try:
                    args_parsed: dict = {}
                    raw_args = tc.get("arguments")
                    if isinstance(raw_args, str) and raw_args.strip():
                        try:
                            args_parsed = json.loads(raw_args)
                        except Exception:
                            args_parsed = {"_raw": raw_args}
                    elif isinstance(raw_args, dict):
                        args_parsed = raw_args
                    tool_ev = ToolCallEvent(
                        session_id=session.session_id,
                        node_id=node_id,
                        tool_name=tc.get("name", ""),
                        tool_args=args_parsed,
                    )
                    await session.emit(tool_ev)
                except Exception:
                    logger.exception("autopsy: emit tool_call failed")
        except Exception:
            logger.exception("autopsy: emit llm_response failed")

        return result

    def _wrap_stream(self, original_stream, session, node_id, model, t0):
        """Wrap an async stream to accumulate content and emit at the end."""
        async def gen():
            content_parts: list[str] = []
            finish_reason = ""
            try:
                async for chunk in original_stream:
                    try:
                        choices = getattr(chunk, "choices", None) or []
                        if choices:
                            delta = getattr(choices[0], "delta", None)
                            c = getattr(delta, "content", None) if delta else None
                            if c:
                                content_parts.append(c)
                            fr = getattr(choices[0], "finish_reason", None)
                            if fr:
                                finish_reason = fr
                    except Exception:
                        pass
                    yield chunk
            finally:
                try:
                    full = "".join(content_parts)
                    resp_ev = LLMResponseEvent(
                        session_id=session.session_id,
                        node_id=node_id,
                        model=str(model),
                        content=full,
                        tool_calls=[],
                        prompt_tokens=0,
                        completion_tokens=len(full) // 4,
                        total_tokens=len(full) // 4,
                        latency_ms=(time.perf_counter() - t0) * 1000,
                        finish_reason=finish_reason or "stop",
                    )
                    await session.emit(resp_ev)
                except Exception:
                    logger.exception("autopsy: stream emit failed")
        return gen()


class InterceptorManager:
    """Globally installed once; uses session refcount to know when to uninstall."""

    _original_create = None
    _active_sessions: set = set()
    _lock_held: bool = False

    @classmethod
    def install(cls, session) -> None:
        cls._active_sessions.add(id(session))
        if cls._original_create is not None:
            return
        try:
            from openai.resources.chat import completions as _c
            target = _c.AsyncCompletions
            cls._original_create = target.create

            async def patched_create(self, *args, **kwargs):
                # Bind the original to its instance and pass through to the wrapper.
                bound_original = cls._original_create.__get__(self, type(self))
                wrapper = _PatchedCreate(bound_original)
                return await wrapper(*args, **kwargs)

            target.create = patched_create
        except Exception:
            logger.exception("autopsy: failed to install OpenAI interceptor")

    @classmethod
    def uninstall(cls, session) -> None:
        cls._active_sessions.discard(id(session))
        if cls._active_sessions:
            return
        if cls._original_create is None:
            return
        try:
            from openai.resources.chat import completions as _c
            _c.AsyncCompletions.create = cls._original_create
        except Exception:
            logger.exception("autopsy: failed to uninstall OpenAI interceptor")
        finally:
            cls._original_create = None
