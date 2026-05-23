"""Data models for all events in the autopsy trace.

Every event emitted by the decorator, interceptor, or diagnostics flows through
these dataclasses. They serialize cleanly to JSON via `dataclasses.asdict` and
round-trip safely.

The TypeScript types in `dashboard/src/types.ts` must mirror these exactly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional
from uuid import uuid4
import time

EventType = Literal[
    "session_start",
    "session_end",
    "node_start",
    "node_end",
    "node_error",
    "llm_request",
    "llm_response",
    "tool_call",
    "tool_result",
    "agent_handoff",
]


@dataclass
class BaseEvent:
    event_id: str = field(default_factory=lambda: str(uuid4()))
    session_id: str = ""
    timestamp: float = field(default_factory=time.time)
    event_type: EventType = "node_start"


@dataclass
class SessionStartEvent(BaseEvent):
    event_type: EventType = "session_start"
    agent_name: str = ""
    input_query: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class NodeStartEvent(BaseEvent):
    event_type: EventType = "node_start"
    node_id: str = ""
    node_type: str = ""  # "llm" | "tool" | "agent" | "user"
    node_name: str = ""
    parent_node_id: Optional[str] = None
    depth: int = 0
    input_data: Any = None


@dataclass
class NodeEndEvent(BaseEvent):
    event_type: EventType = "node_end"
    node_id: str = ""
    duration_ms: float = 0
    output_data: Any = None
    output_hash: str = ""


@dataclass
class NodeErrorEvent(BaseEvent):
    event_type: EventType = "node_error"
    node_id: str = ""
    error_type: str = ""
    error_message: str = ""
    traceback: str = ""
    duration_ms: float = 0


@dataclass
class LLMRequestEvent(BaseEvent):
    event_type: EventType = "llm_request"
    node_id: str = ""
    model: str = ""
    messages: list[dict] = field(default_factory=list)
    temperature: float = 1.0
    max_tokens: int = 0
    tools: list[dict] = field(default_factory=list)
    prompt_tokens_estimate: int = 0


@dataclass
class LLMResponseEvent(BaseEvent):
    event_type: EventType = "llm_response"
    node_id: str = ""
    model: str = ""
    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: float = 0
    finish_reason: str = ""


@dataclass
class ToolCallEvent(BaseEvent):
    event_type: EventType = "tool_call"
    node_id: str = ""
    tool_name: str = ""
    tool_args: dict = field(default_factory=dict)


@dataclass
class ToolResultEvent(BaseEvent):
    event_type: EventType = "tool_result"
    node_id: str = ""
    tool_name: str = ""
    result: Any = None
    error: Optional[str] = None
    latency_ms: float = 0


@dataclass
class AgentHandoffEvent(BaseEvent):
    event_type: EventType = "agent_handoff"
    from_node_id: str = ""
    to_agent_name: str = ""
    handoff_data: Any = None


@dataclass
class SessionEndEvent(BaseEvent):
    event_type: EventType = "session_end"
    total_duration_ms: float = 0
    total_tokens: int = 0
    total_cost_usd: float = 0
    node_count: int = 0
    error_count: int = 0
    status: Literal["success", "error", "partial"] = "success"


@dataclass
class TraceBundle:
    """Complete trace persisted to disk as a replay bundle."""

    session_id: str = ""
    created_at: float = 0.0
    agent_name: str = ""
    input_query: str = ""
    agent_module_path: str = ""
    agent_fn_name: str = ""
    events: list[dict] = field(default_factory=list)
    dag_edges: list[list] = field(default_factory=list)  # [[parent, child], ...]
    node_index: dict = field(default_factory=dict)
    replay_checkpoints: dict = field(default_factory=dict)
    summary: dict = field(default_factory=dict)


@dataclass
class DiagnosisResult:
    """Canonical diagnosis returned by GMIAgent/GeminiAgent."""

    root_cause: str = ""
    affected_node_id: str = ""
    affected_node_name: str = ""
    error_category: str = "other"
    fix_suggestion: str = ""
    fix_code_snippet: str = ""
    confidence: float = 0.0
    latency_insight: str = ""
    estimated_latency_savings_ms: float = 0.0
    model_swap_suggestion: str = ""
    raw_response: str = ""


# Helper: map EventType -> dataclass for safe deserialization
EVENT_TYPE_TO_CLASS: dict[str, type] = {
    "session_start": SessionStartEvent,
    "session_end": SessionEndEvent,
    "node_start": NodeStartEvent,
    "node_end": NodeEndEvent,
    "node_error": NodeErrorEvent,
    "llm_request": LLMRequestEvent,
    "llm_response": LLMResponseEvent,
    "tool_call": ToolCallEvent,
    "tool_result": ToolResultEvent,
    "agent_handoff": AgentHandoffEvent,
}
