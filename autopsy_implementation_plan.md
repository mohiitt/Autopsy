# autopsy — Complete Implementation Plan

> _Your agent died. Here's why._

> **Hand this document to your AI IDE as a single prompt.** It contains every file, interface, data structure, and feature needed to build autopsy end-to-end.

---

## 0. Project Overview

**What we're building:** A pip-installable Python framework (`autopsy`) that wraps any async LLM agent with a single decorator, captures a full execution trace, streams it to a local web dashboard (auto-opened in browser), and runs a GMI Cloud–powered diagnostics agent that identifies root causes and suggests fixes in <2 seconds. The dashboard is deployable as a RocketRide agent for team sharing.

**Stack:**

- Python 3.11+, asyncio, FastAPI, WebSockets
- React + Vite (dashboard frontend, bundled into the package)
- GMI Cloud API (OpenAI-compatible, inference on H100)
- Google Gemini 2.5 Pro (long-context trace analysis)
- RocketRide (one-click deploy of the dashboard agent)

**Pip install and run:**

```bash
pip install autopsy
autopsy run agent.py        # starts server + opens browser
autopsy deploy              # deploys dashboard as RocketRide agent
```

---

## 1. Repository Structure

```
autopsy/
├── README.md
├── pyproject.toml
├── setup.cfg
├── .env.example
│
├── autopsy/                        # main Python package
│   ├── __init__.py                    # exports: lens, LensConfig
│   ├── core/
│   │   ├── __init__.py
│   │   ├── decorator.py               # @lens.trace implementation
│   │   ├── interceptor.py             # OpenAI/httpx hook layer
│   │   ├── tracer.py                  # trace session manager
│   │   ├── events.py                  # all event dataclasses
│   │   └── replay.py                  # replay engine
│   ├── server/
│   │   ├── __init__.py
│   │   ├── app.py                     # FastAPI app
│   │   ├── ws_manager.py              # WebSocket broadcast manager
│   │   ├── routes/
│   │   │   ├── traces.py              # REST: GET /traces, GET /traces/:id
│   │   │   ├── replay.py              # REST: POST /replay
│   │   │   └── diagnose.py            # REST: POST /diagnose
│   │   └── static/                    # built React app (committed)
│   │       ├── index.html
│   │       └── assets/
│   ├── diagnostics/
│   │   ├── __init__.py
│   │   ├── gmi_agent.py               # GMI Cloud inference client
│   │   ├── gemini_agent.py            # Gemini long-context client
│   │   └── prompts.py                 # all system/user prompt templates
│   ├── deploy/
│   │   ├── __init__.py
│   │   └── rocketride.py              # RocketRide deploy client
│   └── cli/
│       ├── __init__.py
│       └── main.py                    # click CLI: run, deploy, replay
│
├── dashboard/                         # React frontend source
│   ├── package.json
│   ├── vite.config.ts
│   ├── tsconfig.json
│   ├── index.html
│   └── src/
│       ├── main.tsx
│       ├── App.tsx
│       ├── types.ts                   # TypeScript types mirroring Python events
│       ├── hooks/
│       │   ├── useTraceSocket.ts      # WebSocket hook
│       │   └── useTraceStore.ts       # Zustand store
│       ├── components/
│       │   ├── DAGGraph.tsx           # agent execution DAG (React Flow)
│       │   ├── NodeDetail.tsx         # side panel: prompt/response/tokens
│       │   ├── DiagnosticsPanel.tsx   # GMI root-cause card
│       │   ├── ReplayControls.tsx     # step-back, fork, re-run controls
│       │   ├── LatencyChart.tsx       # per-node latency bar chart
│       │   ├── TraceList.tsx          # left sidebar: all sessions
│       │   └── TokenCounter.tsx       # running token cost display
│       └── lib/
│           ├── api.ts                 # typed fetch wrappers
│           └── formatters.ts
│
├── tests/
│   ├── unit/
│   │   ├── test_decorator.py
│   │   ├── test_replay.py
│   │   └── test_events.py
│   └── integration/
│       ├── test_server.py
│       └── test_diagnostics.py
│
├── examples/
│   ├── simple_agent.py                # bare openai call
│   ├── langchain_agent.py
│   ├── autogen_agent.py
│   └── broken_agent.py                # demo: intentionally fails for hackathon demo
│
└── scripts/
    └── build_dashboard.sh             # npm run build → copy to autopsy/server/static
```

---

## 2. Data Models — `autopsy/core/events.py`

Every event flowing through the system must match these exact shapes. The React frontend TypeScript types (in `dashboard/src/types.ts`) must mirror these exactly.

```python
from dataclasses import dataclass, field, asdict
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
    node_id: str = ""           # stable hash of (session_id, call_depth, fn_name)
    node_type: str = ""         # "llm" | "tool" | "agent" | "user"
    node_name: str = ""
    parent_node_id: Optional[str] = None
    depth: int = 0
    input_data: Any = None      # serialized input

@dataclass
class NodeEndEvent(BaseEvent):
    event_type: EventType = "node_end"
    node_id: str = ""
    duration_ms: float = 0
    output_data: Any = None
    output_hash: str = ""       # sha256 of serialized output — enables deterministic replay

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

# The complete trace — serialized to disk as replay bundle
@dataclass
class TraceBundle:
    session_id: str
    created_at: float
    agent_name: str
    input_query: str
    agent_module_path: str      # absolute path to the agent's source file (for replay)
    agent_fn_name: str          # fully-qualified function name, e.g. "my_module.research_agent"
    events: list[dict]          # list of asdict(event) in order
    dag_edges: list[tuple]      # [(parent_node_id, child_node_id), ...]
    node_index: dict            # {node_id: {start_event, end_event, llm_events, tool_events}}
    replay_checkpoints: dict    # {node_id: serialized_output_str} — JSON string of each node's output
    summary: dict               # computed: total tokens, latency breakdown, error nodes

# Diagnosis result — canonical definition lives here; both GMIAgent and GeminiAgent return this.
# __init__.py imports it from here. Do NOT redefine it in gmi_agent.py.
@dataclass
class DiagnosisResult:
    root_cause: str                 # 1-2 sentence plain English
    affected_node_id: str
    affected_node_name: str
    error_category: str             # "context_overflow" | "bad_json" | "tool_failure" |
                                    # "hallucination" | "timeout" | "prompt_issue" | "other"
    fix_suggestion: str             # concrete actionable fix
    fix_code_snippet: str           # optional: Python snippet implementing the fix
    confidence: float               # 0.0–1.0
    latency_insight: str            # optional: latency improvement opportunity
    estimated_latency_savings_ms: float
    model_swap_suggestion: str      # optional: "use llama-3-70b at this node to save Xms"
    raw_response: str               # full model response for debug
```

---

## 3. Core Decorator — `autopsy/core/decorator.py`

This is the most critical file. It must work transparently on any async Python function.

```python
"""
Full implementation requirements for @lens.trace decorator.

BEHAVIOUR:
- Wraps an async function and captures all nested LLM calls and tool calls
- Uses contextvars to track call depth and current node_id across nested awaits
- Emits events to TraceSession via asyncio queue
- Works with OpenAI SDK, httpx, LangChain callbacks, and AutoGen
- Adds <1ms overhead in the hot path (event emission is non-blocking)
- Handles exceptions: emits NodeErrorEvent then re-raises

IMPLEMENTATION:
"""
import asyncio
import contextvars
import functools
import hashlib
import importlib.util
import inspect
import json
import sys
import time
from typing import Callable, Any
from uuid import uuid4

from .events import (
    NodeStartEvent, NodeEndEvent, NodeErrorEvent,
    SessionStartEvent, SessionEndEvent
)
from .tracer import get_current_session, set_current_session, TraceSession

# Context variable tracking current node stack
_current_node_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_current_node_id", default=None
)
_call_depth: contextvars.ContextVar[int] = contextvars.ContextVar(
    "_call_depth", default=0
)

def stable_node_id(session_id: str, depth: int, fn_name: str, call_index: int) -> str:
    """Deterministic node ID — same fn at same depth in same session = same ID."""
    raw = f"{session_id}:{depth}:{fn_name}:{call_index}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]

def _serialize_safe(obj: Any) -> Any:
    """Serialize obj to JSON-safe form. Truncate large payloads."""
    try:
        s = json.dumps(obj, default=str)
        if len(s) > 50_000:
            return {"__truncated": True, "size": len(s), "preview": s[:500]}
        return obj
    except Exception:
        return str(obj)

class LensDecorator:
    """
    Usage:
        from autopsy import lens

        @lens.trace                        # use defaults
        async def my_agent(query):  ...

        @lens.trace(name="my-agent")       # with config
        async def my_agent(query):  ...
    """

    def __init__(self, config=None):
        self.config = config or {}

    def trace(self, fn: Callable = None, *, name: str = None):
        """
        Can be used as @lens.trace or @lens.trace(name="...").

        MUST:
        1. Create or join a TraceSession (top-level call creates; nested calls join)
        2. Emit SessionStartEvent at top level, NodeStartEvent always
        3. Capture args/kwargs as input_data
        4. Await the original function
        5. Emit NodeEndEvent with output and output_hash
        6. On exception: emit NodeErrorEvent, then re-raise
        7. At top level: emit SessionEndEvent, persist TraceBundle to disk,
           broadcast final trace via WebSocket to dashboard
        """
        if fn is None:
            # Called as @lens.trace(name="...") — return decorator
            return lambda f: self.trace(f, name=name)

        # Capture module path and fn name at decoration time (not call time)
        _agent_module_path = inspect.getfile(fn)
        _agent_fn_name = f"{fn.__module__}.{fn.__qualname__}"

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            session = get_current_session()
            is_root = session is None
            if is_root:
                session = TraceSession(
                    agent_name=name or fn.__name__,
                    input_query=str(args[0]) if args else str(kwargs),
                    agent_module_path=_agent_module_path,
                    agent_fn_name=_agent_fn_name,
                )
                set_current_session(session)    # make session visible to nested calls
                # patches OpenAI client + httpx transport on session
                session.install_interceptors()

            depth = _call_depth.get()
            node_id = str(uuid4())[:8]
            parent_id = _current_node_id.get()

            start_time = time.perf_counter()
            start_event = NodeStartEvent(
                session_id=session.session_id,
                node_id=node_id,
                node_type="agent",
                node_name=name or fn.__name__,
                parent_node_id=parent_id,
                depth=depth,
                input_data=_serialize_safe({"args": args, "kwargs": kwargs}),
            )
            await session.emit(start_event)

            token = _current_node_id.set(node_id)
            depth_token = _call_depth.set(depth + 1)
            try:
                result = await fn(*args, **kwargs)
                duration = (time.perf_counter() - start_time) * 1000
                end_event = NodeEndEvent(
                    session_id=session.session_id,
                    node_id=node_id,
                    duration_ms=duration,
                    output_data=_serialize_safe(result),
                    output_hash=hashlib.sha256(
                        json.dumps(result, default=str).encode()
                    ).hexdigest()[:12],
                )
                await session.emit(end_event)
                return result
            except Exception as exc:
                duration = (time.perf_counter() - start_time) * 1000
                import traceback as tb
                error_event = NodeErrorEvent(
                    session_id=session.session_id,
                    node_id=node_id,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    traceback=tb.format_exc(),
                    duration_ms=duration,
                )
                await session.emit(error_event)
                raise
            finally:
                _current_node_id.reset(token)
                _call_depth.reset(depth_token)
                if is_root:
                    await session.finalize()
                    session.uninstall_interceptors()
                    set_current_session(None)   # clear session so next top-level call starts fresh

        return wrapper
```

---

## 4. OpenAI Interceptor — `autopsy/core/interceptor.py`

This intercepts all OpenAI SDK calls transparently, without requiring users to change their code.

```python
"""
IMPLEMENTATION REQUIREMENTS:

The interceptor must monkey-patch the OpenAI AsyncClient's chat.completions.create
method to emit LLMRequestEvent before the call and LLMResponseEvent after.

Strategy:
1. Store original openai.AsyncOpenAI.chat.completions.create
2. Replace with an async wrapper that:
   a. Emits LLMRequestEvent with messages, model, tools, estimated prompt tokens
   b. Calls original create()
   c. On streaming response: accumulates chunks, emits LLMResponseEvent when stream ends
   d. On non-streaming: emits LLMResponseEvent immediately
   e. On error: still emits what it has, sets finish_reason="error"
3. Uses the current session from contextvars (get_current_session())
4. If no session active, passes through without modification (safe for non-traced code)

Token estimation (prompt_tokens_estimate before the API call):
   - For OpenAI models (gpt-*, o1-*, o3-*): use tiktoken.encoding_for_model(model)
   - For all other models (LLaMA, Gemini, etc.): use a universal fallback:
       estimate = sum(len(m["content"]) for m in messages) // 4
     (approx 4 chars per token — accurate to within 15% for all major models)
   - Never hard-fail if tiktoken doesn't recognise the model: catch the exception
     and fall back to the char-based estimate.

httpx transport-level fallback (catches non-OpenAI SDK callers):
- Install a custom httpx event hook on the default client at session start
- If request URL matches known LLM provider domains:
    openai.com, api.gmi-serving.com, generativelanguage.googleapis.com,
    api.anthropic.com, api.mistral.ai, api.together.xyz
  then parse request body as JSON, extract messages/model/tools
  and parse response body to extract usage/content.
- This catches LangChain, AutoGen, and any raw httpx callers transparently.

LangChain-specific strategy:
- LangChain uses a callback system. Register a custom BaseCallbackHandler:
    class AutopsyLangChainCallback(BaseCallbackHandler):
        def on_llm_start(self, serialized, messages, **kwargs): emit LLMRequestEvent
        def on_llm_end(self, response, **kwargs): emit LLMResponseEvent
        def on_tool_start(self, serialized, input_str, **kwargs): emit ToolCallEvent
        def on_tool_end(self, output, **kwargs): emit ToolResultEvent
        def on_tool_error(self, error, **kwargs): emit ToolResultEvent(error=str(error))
- Inject this handler into any LangChain chain found in the call stack via:
    autopsy.integrations.langchain.inject_callback(chain, session)
- Users can also add it manually: chain.callbacks.append(AutopsyLangChainCallback())
- httpx fallback still covers LangChain as a safety net.

AutoGen-specific strategy:
- AutoGen routes all LLM calls through OpenAI SDK — the OpenAI monkey-patch
  covers it automatically.
- For agent handoffs between AutoGen agents, detect them by patching
  autogen.ConversableAgent.initiate_chat and emit AgentHandoffEvent.

Tool call detection:
- Tool calls are detected from the LLM response object:
    if response.choices[0].message.tool_calls:
        for tc in response.choices[0].message.tool_calls:
            emit ToolCallEvent(tool_name=tc.function.name, tool_args=json.loads(tc.function.arguments))
- ToolResultEvent is emitted by wrapping the Python function that handles the
  tool call. Strategy: if a function decorated with @tool (LangChain) or
  registered as a tool in the assistant's tools list is called, time its
  execution and emit ToolResultEvent with result or error.
  Fallback: if tool result appears as a subsequent tool role message in the
  next LLM request, parse it and backfill a ToolResultEvent retroactively.

IMPORTANT: The interceptor must be re-entrant safe. If the diagnostics agent itself
makes LLM calls, those must NOT be traced into the user's session.
Use a context var `_in_diagnostics_call` to suppress tracing during diagnosis.
"""
```

---

## 5. Trace Session Manager — `autopsy/core/tracer.py`

```python
"""
TraceSession — manages lifetime of one agent run.

RESPONSIBILITIES:
1. Holds the asyncio.Queue for events from decorator and interceptors
2. Runs background task that drains queue and:
   a. Appends to self.events list
   b. Broadcasts each event as JSON to WebSocket manager
   c. Builds self.dag_edges as (parent_id, child_id) pairs from NodeStart events
   d. Builds self.node_index keyed by node_id
3. finalize():
   a. Computes TraceBundle.summary (total tokens, latency per node, error nodes list)
   b. Saves TraceBundle to ~/.autopsy/sessions/{session_id}.json
   c. Also saves to .autopsy/ in current working directory
   d. Emits session_end WebSocket message with full bundle
4. install_interceptors() / uninstall_interceptors():
   a. Calls InterceptorManager.install(self) — passes session ref to interceptors
5. Replay checkpoints:
   a. After each NodeEndEvent, snapshot the current state of all inputs seen so far
   b. Store as self.replay_checkpoints[node_id] = {all prior node outputs, inputs}
   c. This allows replay from any node

get_current_session() / set_current_session(session: TraceSession | None):
   _current_session: ContextVar[TraceSession | None] = ContextVar("_current_session", default=None)
   def get_current_session() -> TraceSession | None:
       return _current_session.get()
   def set_current_session(session: TraceSession | None) -> None:
       _current_session.set(session)
   MUST be called by the decorator:
     - set_current_session(session) immediately after creating TraceSession
     - set_current_session(None) in the is_root finally block after finalize()

Session index concurrency safety:
   Use `filelock` library (add "filelock>=3.13" to dependencies).
   Lock file: ~/.autopsy/sessions/sessions_index.lock
   Write pattern:
     1. Acquire exclusive FileLock on sessions_index.lock
     2. Read current sessions_index.json (or [] if absent)
     3. Append new session summary dict
     4. Write to sessions_index.json.tmp
     5. os.replace(tmp, sessions_index.json)  # atomic rename
     6. Release lock
   This is safe for concurrent `autopsy run` invocations in the same directory.

SESSION STORAGE FORMAT (~/.autopsy/sessions/):
   {session_id}.json — full TraceBundle as JSON
   sessions_index.json — [{session_id, agent_name, created_at, status, error_count}]
   sessions_index.lock — filelock file (never commit to git)
"""
```

---

## 6. Replay Engine — `autopsy/core/replay.py`

```python
"""
ReplayEngine — re-execute any agent from any node checkpoint.

API:
    engine = ReplayEngine(bundle: TraceBundle)

    # Re-run the full session with different config
    result = await engine.replay_full(model_override="llama-3-70b")

    # Re-run from a specific node (everything before is frozen/mocked)
    result = await engine.replay_from_node(
        node_id="a3f2c1",
        model_override=None,
        temperature_override=None,
        prompt_patch=None,       # optional: replace system prompt at this node
    )

    # Compare two runs side-by-side
    comparison = await engine.compare(
        run_a_config={},
        run_b_config={"model_override": "llama-3-70b"}
    )

IMPLEMENTATION:

Replay mode uses two module-level contextvars:
  _REPLAY_MODE:    ContextVar[bool] = ContextVar("_REPLAY_MODE", default=False)
  _REPLAY_OUTPUTS: ContextVar[dict] = ContextVar("_REPLAY_OUTPUTS", default={})
    # dict maps node_id → JSON string of that node's frozen output (from replay_checkpoints)

replay_from_node() steps:
  1. Build the "frozen set": all node_ids in DAG execution order BEFORE target_node_id
     (use dag_edges topological order to determine which nodes precede the target).
  2. Populate frozen_outputs = {node_id: bundle.replay_checkpoints[node_id]
                                for node_id in frozen_set}
  3. Set contextvars: _REPLAY_MODE=True, _REPLAY_OUTPUTS=frozen_outputs
  4. Load the agent function via importlib:
       spec = importlib.util.spec_from_file_location(
           bundle.agent_fn_name.split(".")[0], bundle.agent_module_path
       )
       module = importlib.util.module_from_spec(spec)
       spec.loader.exec_module(module)
       fn = getattr(module, bundle.agent_fn_name.split(".")[-1])
  5. Call fn with original args from SessionStartEvent.input_query.
  6. The @lens.trace decorator checks _REPLAY_MODE at the top of each wrapper call:
       if _REPLAY_MODE.get() and node_id in _REPLAY_OUTPUTS.get():
           frozen = json.loads(_REPLAY_OUTPUTS.get()[node_id])
           # emit a NodeStartEvent + NodeEndEvent with frozen data (for the replay trace)
           # return the deserialized frozen output WITHOUT executing the real function
           return frozen
  7. The interceptor also checks _REPLAY_OUTPUTS using the parent node_id:
     if a LLM call is made inside a frozen node, it returns a mock LLMResponseEvent
     built from the frozen LLMResponseEvent stored in node_index[node_id]["llm_events"].
  8. At and after the target node: real execution proceeds normally.
  9. All events emitted during replay use a new ReplayTraceSession with:
       session_id = f"replay:{original_session_id}:{node_id[:6]}"
     So the dashboard can display the replay trace alongside the original.

SIDE EFFECT WARNING (mandatory — must be shown in CLI and dashboard):
  Replay mocks node outputs and LLM calls for all nodes before the checkpoint.
  External tool side effects (file writes, DB mutations, API calls with side
  effects) are NOT mocked — they occurred in the original run and may be
  replayed if the agent re-executes those code paths after the checkpoint.
  Before running replay_from_node(), autopsy CLI must print:
    "WARNING: Replay mocks LLM I/O only. External side effects (file writes,
     API mutations) in nodes AT or AFTER the target node will re-execute.
     Proceed? [y/N]"
  Dashboard shows a non-dismissable amber banner during replay:
    "Replay mode: nodes before checkpoint are mocked. Side effects may occur."

COMPARISON OUTPUT:
    {
      "node_id": "a3f2c1",
      "run_a": {"output": ..., "latency_ms": 340, "tokens": 512},
      "run_b": {"output": ..., "latency_ms": 180, "tokens": 430},
      "diff": {
        "output_changed": true,
        "latency_delta_ms": -160,
        "token_delta": -82,
        "output_diff": "..."  # unified diff of string outputs
      }
    }
"""
```

---

## 7. FastAPI Server — `autopsy/server/app.py`

```python
"""
FastAPI application serving:

REST ENDPOINTS:
  GET  /api/sessions                          — list all saved sessions (from index)
  GET  /api/sessions/{session_id}             — full TraceBundle JSON
  GET  /api/sessions/{session_id}/dag         — just dag_edges + node_index summary
  POST /api/sessions/{session_id}/diagnose    — trigger GMI/Gemini diagnostics
  POST /api/sessions/{session_id}/replay      — body: {node_id, model_override, prompt_patch}
  POST /api/sessions/{session_id}/compare     — body: {node_id, config_a, config_b}
  GET  /health                                — {status: "ok", version: "..."}

WEBSOCKET:
  WS /ws/live
    — clients subscribe here
    — server broadcasts every event emitted during active agent run
    — message format: {"type": "event", "data": <serialized event>}
    — on session_end: {"type": "session_complete", "data": <TraceBundle.summary ONLY>}
      Rationale: full TraceBundle can be several MB. Sending it over WS causes
      browser freezes and potential connection drops. The dashboard fetches the
      full bundle via REST (GET /api/sessions/{session_id}) after receiving
      session_complete, using the session_id from the summary.
    — on server start: {"type": "sessions_list", "data": [...sessions index...]}

STATIC FILES:
  Mount the built React app at / (catch-all, serve index.html for all non-/api routes)

STARTUP:
  - Load existing sessions index from ~/.autopsy/sessions/sessions_index.json
  - Broadcast sessions_list to any connected WS clients

CORS: allow all origins in development mode (for autopsy deploy / RocketRide)
"""
```

---

## 8. WebSocket Manager — `autopsy/server/ws_manager.py`

```python
"""
WSManager — manages all connected WebSocket clients.

class WSManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket)
    async def disconnect(self, ws: WebSocket)
    async def broadcast(self, message: dict)       # JSON-serializes and sends to all
    async def broadcast_event(self, event: BaseEvent)   # wraps in {"type":"event","data":...}

SINGLETON: expose as module-level `ws_manager = WSManager()`
TraceSession holds a reference to ws_manager and calls broadcast_event() on every emit.
"""
```

---

## 9. Diagnostics Agent — `autopsy/diagnostics/gmi_agent.py`

```python
"""
GMIAgent — calls GMI Cloud to diagnose a failed trace.

SETUP:
    GMI Cloud base_url: https://api.gmi-serving.com/v1
    Uses OpenAI SDK pointed at GMI base_url with GMI_API_KEY
    Default model: "meta-llama/Llama-4-Maverick-17B-128E-Instruct"  (fast, strong reasoning)
    Fallback model: "deepseek-ai/DeepSeek-R1-0528"       (for complex traces)

class GMIAgent:
    async def diagnose(self, bundle: TraceBundle, target_node_id: str = None) -> DiagnosisResult:
        '''
        If target_node_id is None, auto-detect the first error node from bundle.

        Build context:
          1. Summarize the full DAG as text: node names, types, sequence, latencies
          2. For the failed node: full prompt, response, error message, traceback
          3. For parent nodes of the failed node: their outputs (as the failed node's context)
          4. Token budget: stay under 8k tokens for the diagnosis prompt

        Call GMI with the diagnosis system prompt (see prompts.py).
        Parse the structured JSON response.
        Return DiagnosisResult.
        '''

@dataclass
class DiagnosisResult:
    # REMOVED — DiagnosisResult is now defined in autopsy/core/events.py.
    # Import it from there:
    pass

from autopsy.core.events import DiagnosisResult  # noqa: E402
```

---

## 10. Diagnostics Prompts — `autopsy/diagnostics/prompts.py`

```python
"""
DIAGNOSIS_SYSTEM_PROMPT = '''
You are an expert AI agent debugger. You receive a trace of a multi-agent LLM pipeline
that has failed or performed poorly, and you diagnose the root cause.

You MUST respond with a valid JSON object matching this exact schema:
{
  "root_cause": "<1-2 sentences, plain English, no jargon>",
  "affected_node_id": "<node_id string>",
  "affected_node_name": "<human readable name>",
  "error_category": "<one of: context_overflow, bad_json, tool_failure, hallucination, timeout, prompt_issue, other>",
  "fix_suggestion": "<concrete actionable fix in plain English>",
  "fix_code_snippet": "<optional Python code snippet>",
  "confidence": <0.0 to 1.0>,
  "latency_insight": "<optional: what is eating the most latency>",
  "estimated_latency_savings_ms": <number or 0>,
  "model_swap_suggestion": "<optional: suggest model swap at a specific node>"
}

Rules:
- Be specific. Reference the actual node name and what it received.
- The fix_suggestion must be something the developer can implement in under 10 minutes.
- If the error is context_overflow, always mention the specific token count and threshold.
- If you are unsure, lower confidence and say so in root_cause.
- Do not include any text outside the JSON object.
'''

def build_diagnosis_user_prompt(bundle: TraceBundle, target_node_id: str) -> str:
    # Builds the user message with:
    # 1. DAG summary (all nodes as numbered list with types and latencies)
    # 2. Failed node detail (name, prompt, response, error, traceback)
    # 3. Parent node outputs
    # 4. Token usage summary
    # Returns formatted string under 6000 tokens
    ...

LATENCY_ANALYSIS_PROMPT = '''
Analyze the latency breakdown below and identify:
1. The single biggest latency bottleneck
2. Whether the bottleneck is avoidable (e.g. redundant tool calls, large context)
3. A specific recommendation to reduce latency by at least 20%

Respond in JSON: {"bottleneck_node": "...", "bottleneck_reason": "...", "recommendation": "...", "estimated_savings_ms": ...}
'''
"""
```

---

## 11. Gemini Long-Context Agent — `autopsy/diagnostics/gemini_agent.py`

```python
"""
GeminiAgent — used for traces > 32k tokens (falls back from GMI).

Uses Google AI Studio API (Gemini 2.5 Pro, 1M context window).
API key from env: GOOGLE_AI_API_KEY

class GeminiAgent:
    async def diagnose(self, bundle: TraceBundle, target_node_id: str) -> DiagnosisResult:
        # Same interface as GMIAgent
        # Serializes full bundle as text (all events, all prompts/responses)
        # Calls Gemini 1.5 Pro with the full trace + DIAGNOSIS_SYSTEM_PROMPT
        # Parses JSON response into DiagnosisResult

    async def summarize_long_trace(self, bundle: TraceBundle) -> str:
        # Returns a 500-word summary of the full trace for display in dashboard
        # Used when trace is too long to show in full in the side panel

SELECTION LOGIC (in diagnose.py route):
    if bundle_token_estimate > 32_000:
        agent = GeminiAgent()
    else:
        agent = GMIAgent()
    result = await agent.diagnose(bundle, node_id)
"""
```

---

## 12. CLI — `autopsy/cli/main.py`

```python
"""
Click CLI with these commands:

autopsy run <script.py> [--port 7823] [--no-browser] [--debug]
    1. Import the script as a module (use importlib + runpy)
    2. Start FastAPI server on given port (uvicorn, in background thread)
    3. Open browser to http://localhost:{port} after 500ms delay
    4. Keep running until Ctrl+C
    5. On exit: print session summary (total runs, total tokens, any errors)

autopsy deploy [--name "my-agent-dashboard"] [--rocketride-key KEY]
    1. Build the RocketRide agent config (see deploy/rocketride.py)
    2. Upload the current session store to RocketRide
    3. Return a shareable URL

autopsy replay <session_id_or_path> [--from-node NODE_ID] [--model MODEL]
    1. Load the TraceBundle from disk
    2. Run ReplayEngine.replay_from_node()
    3. Open browser to show replay vs original comparison

autopsy diagnose <session_id_or_path> [--node NODE_ID]
    1. Load TraceBundle
    2. Call GMIAgent.diagnose()
    3. Pretty-print DiagnosisResult to terminal (rich formatting)

autopsy sessions
    1. List all saved sessions from ~/.autopsy/sessions/sessions_index.json
    2. Show: session_id, agent_name, date, status, token count, error count

autopsy clean [--older-than 7d]
    1. Delete old session files from ~/.autopsy/sessions/

Note: graceful shutdown on Ctrl+C — the server waits up to 5 seconds for any
in-flight trace session to call finalize() before exiting. This ensures the
last session is persisted even if the agent was still running.

ENV VARS READ (all prefixed AUTOPSY_ to match package name):
    AUTOPSY_PORT=7823
    AUTOPSY_HOST=127.0.0.1
    GMI_API_KEY=...
    GOOGLE_AI_API_KEY=...
    ROCKETRIDE_API_KEY=...
    AUTOPSY_DEBUG=0
"""
```

---

## 13. RocketRide Deploy — `autopsy/deploy/rocketride.py`

```python
"""
RocketRide integration — deploys the autopsy dashboard as a RocketRide agent.

STATUS: This feature is scoped to v2 (post-hackathon). For v1, `autopsy deploy`
will generate a static shareable JSON export of the current sessions that can
be opened locally by teammates using `autopsy open <path.json>`.

For v2 full deploy, the integration will work as follows:

class RocketRideDeployer:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.rocketride.io/v1"   # confirm exact endpoint with RocketRide docs before v2 implementation

    # v1 fallback (available now):
    async def export_sessions(self, sessions_dir: str, output_path: str) -> str:
        '''
        Bundles all session JSON files into a single autopsy-export.json.
        Teammate runs: autopsy open autopsy-export.json to view in local dashboard.
        Returns output_path.
        '''

    async def deploy(self, name: str, sessions_dir: str) -> DeployResult:
        '''
        1. Package the FastAPI server + static dashboard + sessions into a deploy bundle
        2. POST to RocketRide deploy endpoint with the bundle + agent config
        3. Agent config specifies:
           - runtime: python3.11
           - entrypoint: autopsy.server.app:app
           - env vars: GMI_API_KEY, GOOGLE_AI_API_KEY (from local env)
           - expose: port 8080
        4. Return {url, agent_id, status}
        '''

@dataclass
class DeployResult:
    url: str
    agent_id: str
    status: str
    deploy_log: str

ROCKETRIDE_AGENT_CONFIG = {
    "name": "{name}",
    "runtime": "python3.11",
    "entrypoint": "autopsy.server.app:create_app",
    "port": 8080,
    "env": ["GMI_API_KEY", "GOOGLE_AI_API_KEY"],
    "description": "autopsy trace dashboard — AI-powered agent observability"
}
"""
```

---

## 14. React Dashboard — Full Component Spec

### `dashboard/src/App.tsx`

```
Layout:
  ┌─────────────────────────────────────────────────────┐
  │  Header: "autopsy" logo + live indicator dot     │
  ├──────────┬──────────────────────────┬───────────────┤
  │ TraceList│     DAGGraph             │  NodeDetail   │
  │ (left    │     (center, main)       │  (right panel)│
  │  sidebar)│                          │               │
  │          │                          │  [Diagnose]   │
  │          │                          │  [Replay]     │
  ├──────────┴──────────────────────────┴───────────────┤
  │  LatencyChart (bottom strip) + TokenCounter         │
  └─────────────────────────────────────────────────────┘

State managed in Zustand store (useTraceStore):
  - sessions: TraceBundle[]
  - activeSessionId: string
  - activeNodeId: string | null
  - liveEvents: BaseEvent[]           // streaming during active run
  - diagnosisResult: DiagnosisResult | null
  - diagnosisLoading: boolean
  - replayState: null | "running" | ReplayComparison
```

### `dashboard/src/components/DAGGraph.tsx`

```
Library: React Flow (reactflow package)

NODE RENDERING:
  - Each node_id from node_index → one React Flow node
  - Node color:
      green (#0a6e55) if NodeEndEvent present and no NodeErrorEvent
      red (#c0392b) if NodeErrorEvent present
      amber (#9a6000) if running (NodeStartEvent seen, no end yet)
      grey if blocked (parent errored)
  - Node label: node_name + node_type icon
  - Node subtitle: latency_ms if complete, "..." if running
  - Selected node: blue border, triggers NodeDetail panel

EDGES:
  - Derived from dag_edges list
  - Animated dashed line during active run
  - Solid line once session complete

LIVE UPDATE:
  - On each WebSocket event, update node colors in real-time
  - Animate node appearance with fade-in
  - Auto-layout using dagre (top-to-bottom hierarchical layout)

INTERACTIONS:
  - Click node → set activeNodeId → shows NodeDetail
  - Right-click node → context menu: "Diagnose from here", "Replay from here", "Copy node ID"
  - Zoom/pan: React Flow built-in
```

### `dashboard/src/components/NodeDetail.tsx`

```
Right panel showing full detail of selected node.

SECTIONS (tabs):
  1. Overview
     - node_name, node_type badge, status badge
     - depth indicator (indented to show nesting)
     - duration_ms, tokens used (prompt + completion)
     - parent node name (clickable → selects parent)

  2. Input / Output
     - input_data: syntax-highlighted JSON (use react-json-view)
     - output_data: syntax-highlighted JSON
     - If NodeErrorEvent: show error_type, error_message in red box, traceback in expandable code block

  3. LLM Calls (if any LLMRequest/LLMResponseEvents for this node)
     - Model name + badge
     - Full messages array (each message as expandable chat bubble)
     - Tool calls list
     - Token counts: prompt / completion / total
     - Latency bar

  4. Diagnostics (shown when DiagnosisResult is loaded for this node)
     - Root cause card (styled prominently)
     - Error category badge
     - Fix suggestion (markdown rendered)
     - Code snippet (syntax highlighted, copy button)
     - Confidence meter (0–100%)
     - Latency insight

ACTIONS (bottom of panel):
  [Diagnose this node]  → POST /diagnose, show loading spinner, populate tab 4
  [Replay from here]    → opens ReplayControls with this node pre-selected
  [Copy node ID]        → clipboard
```

### `dashboard/src/components/DiagnosticsPanel.tsx`

```
Shown as an overlay card when diagnosis is complete.

LAYOUT:
  ┌─────────────────────────────────────────┐
  │ 🔍 Root Cause                           │
  │ <root_cause text>                       │
  │                                         │
  │ Node: <affected_node_name>   [go to ↗]  │
  │ Category: <badge>    Confidence: ██░░ 72%│
  │                                         │
  │ Fix                                     │
  │ <fix_suggestion>                        │
  │                                         │
  │ ┌─ Code ─────────────────────────────┐  │
  │ │ <fix_code_snippet>          [copy] │  │
  │ └────────────────────────────────────┘  │
  │                                         │
  │ ⚡ Latency: <latency_insight>           │
  │   Est. savings: -340ms                  │
  │                                         │
  │ [Apply fix & Replay]   [Dismiss]        │
  └─────────────────────────────────────────┘

"Apply fix & Replay" button:
  - If fix_code_snippet is present, shows a mini editor (Monaco or CodeMirror)
    where user can edit the snippet before replaying
  - On confirm: POST /replay with the patch
```

### `dashboard/src/components/ReplayControls.tsx`

```
Bottom drawer that slides up when replay is triggered.

CONTROLS:
  - Node selector dropdown: "Replay from: [dropdown of all nodes]"
  - Model override input: "Use model: [text input, default: same as original]"
  - Temperature slider: 0.0 – 2.0
  - Prompt patch textarea: optional system prompt override
  - [Run Replay] button
  - [Compare side-by-side] toggle

DURING REPLAY:
  - Show progress: "Replaying from node X... (mocking 3 prior nodes)"
  - DAGGraph shows replay trace in blue alongside original in grey

COMPARISON VIEW:
  After replay complete, show a diff table:
  Node | Original Output | Replay Output | Latency Δ | Token Δ
  Each row expandable to show full output diff.
```

### `dashboard/src/components/LatencyChart.tsx`

```
Bottom strip bar chart.

Library: Recharts (BarChart)

DATA: one bar per node, height = duration_ms, color = node status color
X-axis: node names (truncated)
Y-axis: milliseconds
Tooltip: full node name + exact ms + % of total

CLICK: clicking a bar selects that node (same as clicking DAG node)

Shows a horizontal line at p50 latency for context.
```

### Empty and Error States (required for all components)

```
TraceList — empty state:
  When sessions_index is empty: show a centered message:
    "No traces yet."
    "Run: autopsy run agent.py to start recording."
  Show a subtle animated pulse on the live indicator dot while connected.

DAGGraph — empty/connecting state:
  While WebSocket is connecting: show a centered spinner + "Connecting to autopsy server..."
  If WebSocket fails after 3 retries: show a red banner
    "Cannot connect to autopsy server at localhost:7823. Is it running?"
  If session is selected but has no nodes yet: show "Waiting for first event..."

NodeDetail — no node selected:
  Show placeholder: "Select a node in the graph to inspect it."

Diagnosis — timeout:
  If POST /diagnose does not respond within 30 seconds, show:
    "Diagnosis is taking longer than expected. GMI Cloud may be busy."
    [Retry] [Cancel]

Replay — in-progress state:
  Show a progress bar: "Replaying from node X... (mocking N prior nodes)"
  If replay fails (exception from agent): show the error in red with the
  NodeErrorEvent details.

General network errors:
  Any failed API call shows a dismissable toast: "⚠️ [endpoint] failed: [status]"
  with a Retry button.
```

### `dashboard/src/hooks/useTraceSocket.ts`

```typescript
/*
WebSocket hook that:
1. Connects to ws://localhost:7823/ws/live on mount
2. Dispatches incoming messages to Zustand store actions:
   - "event" → store.addLiveEvent(event)
   - "session_complete" → on receiving summary, fetch full bundle via
     GET /api/sessions/{session_id}, then call store.addSession(bundle)
     and store.clearLiveEvents()
   - "sessions_list" → store.setSessions(sessions)
3. Reconnects automatically on disconnect (exponential backoff, max 5s)
4. Exposes: { connected: boolean, lastEventAt: number }
*/
```

---

## 15. pyproject.toml

```toml
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "autopsy"
version = "0.1.0"
description = "Your agent died. Here's why. — Zero-config observability & failure replay for agentic LLM apps"
readme = "README.md"
requires-python = ">=3.11"
license = {text = "MIT"}
keywords = ["llm", "agents", "observability", "tracing", "debugging", "openai"]
classifiers = [
    "Development Status :: 3 - Alpha",
    "Intended Audience :: Developers",
    "Topic :: Software Development :: Debuggers",
    "Programming Language :: Python :: 3.11",
]

dependencies = [
    "fastapi>=0.111.0",
    "uvicorn[standard]>=0.29.0",
    "websockets>=12.0",
    "openai>=1.30.0",
    "httpx>=0.27.0",
    "click>=8.1.0",
    "rich>=13.0.0",
    "google-generativeai>=0.7.0",
    "pydantic>=2.0.0",
    "aiofiles>=23.0.0",
    "python-dotenv>=1.0.0",
    "tiktoken>=0.7.0",      # token estimation for OpenAI models; char fallback used for others
    "filelock>=3.13.0",    # safe concurrent session index writes
]

[project.optional-dependencies]
langchain = ["langchain>=0.2.0", "langchain-openai>=0.1.0"]
autogen = ["pyautogen>=0.2.0"]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23", "httpx", "respx>=0.21", "ruff", "mypy"]

[project.scripts]
autopsy = "autopsy.cli.main:cli"

[tool.setuptools.packages.find]
where = ["."]
include = ["autopsy*"]

[tool.setuptools.package-data]
"autopsy.server" = ["static/**/*"]
```

---

## 16. Environment Variables — `.env.example`

```bash
# GMI Cloud — get at gmi.ai/console
GMI_API_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpZCI6Ijg2MTJmYTJhLTliMmUtNDA3YS04ZDk2LTBmMDRiMjRjMTcyMSIsInNjb3BlIjoiaWVfbW9kZWwiLCJjbGllbnRJZCI6IjAwMDAwMDAwLTAwMDAtMDAwMC0wMDAwLTAwMDAwMDAwMDAwMCJ9.IHqMwbfEJEj8CgGXQaqN9u2xeQGFBcaKo1VCcbDoCdY
GMI_BASE_URL=https://api.gmi-serving.com/v1
GMI_DEFAULT_MODEL=meta-llama/Llama-4-Maverick-17B-128E-Instruct
GMI_FALLBACK_MODEL=deepseek-ai/DeepSeek-R1-0528

# Google AI Studio — get at aistudio.google.com
GOOGLE_AI_API_KEY=AIzaSyDv9Yd-ZbxB45OtxsTvWGOb1PFc9d0d0cI
GEMINI_MODEL=gemini-2.5-pro

# RocketRide — get at rocketride.io
ROCKETRIDE_API_KEY=your_rocketride_key_here

# autopsy server config
AUTOPSY_PORT=7823
AUTOPSY_HOST=127.0.0.1
AUTOPSY_SESSION_DIR=~/.autopsy/sessions
AUTOPSY_MAX_SESSIONS=100
AUTOPSY_DEBUG=0

# Optional: disable auto-browser open
AUTOPSY_NO_BROWSER=0
```

---

## 17. Example Agent Files — `examples/broken_agent.py`

This is the **hackathon demo file**. It must fail in a controlled, visually interesting way.

```python
"""
Broken agent for hackathon demo.
Run with: autopsy run examples/broken_agent.py

What it does:
1. Takes a hardcoded query: "Summarize the latest news about AI safety"
2. Planner agent (GMI LLaMA) decides to search + summarize
3. Search tool: returns a large fake news article (~4000 tokens)
4. Summarizer agent (same GMI model): receives TOO MUCH context
   — the Search output + original messages exceeds the model's practical limit
   — model returns malformed JSON (simulating a real failure mode)
5. Response assembler: fails to parse the malformed JSON → raises ValueError

This triggers:
- NodeErrorEvent on the Summarizer node
- NodeErrorEvent on the Response Assembler node
- Session ends with status="error"
- Dashboard shows two red nodes
- Diagnose button → GMI identifies context overflow + bad JSON as root cause
- Replay from Summarizer with chunk_size fix → both nodes go green
"""

import asyncio
import json
import os
from openai import AsyncOpenAI
from autopsy import lens

client = AsyncOpenAI(
    base_url="https://api.gmi-serving.com/v1",
    api_key=os.environ["GMI_API_KEY"]
)

LARGE_FAKE_ARTICLE = "..." * 400   # ~4000 tokens of fake news text

async def search_tool(query: str) -> str:
    # Simulates a tool that returns too much data
    return LARGE_FAKE_ARTICLE

async def summarizer_agent(context: str) -> dict:
    # Guaranteed deterministic failure: if context exceeds threshold, raise immediately
    # without making an API call. This makes the demo failure 100% reliable and free.
    CONTEXT_LIMIT_CHARS = 8000   # ~2000 tokens — well within model limits but we enforce lower
    if len(context) > CONTEXT_LIMIT_CHARS:
        # Simulate the failure mode a real model would produce with overloaded context:
        # truncated output that breaks JSON parsing.
        raise json.JSONDecodeError(
            "Context overflow: simulated malformed JSON from overloaded model",
            doc="{\"summary\": \"AI safety is",  # truncated mid-string, invalid JSON
            pos=23,
        )
    response = await client.chat.completions.create(
        model="meta-llama/Llama-4-Maverick-17B-128E-Instruct",
        messages=[
            {"role": "system", "content": "Summarize and return JSON: {\"summary\": \"...\", \"key_points\": [...]}"},
            {"role": "user", "content": context}
        ],
        max_tokens=500
    )
    raw = response.choices[0].message.content
    return json.loads(raw)

async def response_assembler(summary_data: dict) -> str:
    return f"Summary: {summary_data['summary']}"

@lens.trace(name="news-research-agent")
async def research_agent(query: str):
    # Step 1: plan
    plan_response = await client.chat.completions.create(
        model="meta-llama/Llama-4-Maverick-17B-128E-Instruct",
        messages=[{"role": "user", "content": f"Plan how to research: {query}"}],
        max_tokens=200
    )

    # Step 2: search (returns too much data)
    search_results = await search_tool(query)

    # Step 3: summarize (will fail)
    summary = await summarizer_agent(search_results)

    # Step 4: assemble (blocked by step 3 failure)
    return await response_assembler(summary)

if __name__ == "__main__":
    asyncio.run(research_agent("Summarize the latest news about AI safety"))
```

---

## 18. Build & Release Script — `scripts/build_dashboard.sh`

```bash
#!/bin/bash
# Run this before publishing to PyPI to bundle the React dashboard into the package.
set -e

echo "Building React dashboard..."
cd dashboard
npm install
npm run build

echo "Copying built assets to package..."
rm -rf ../autopsy/server/static
cp -r dist ../autopsy/server/static

echo "Dashboard built and copied to autopsy/server/static/"
echo "Run: pip install -e . to install locally"
```

---

## 19. Build Order for AI IDE

Build in this exact sequence to avoid import errors:

```
1.  autopsy/core/events.py              (no deps)
2.  autopsy/core/tracer.py              (deps: events)
3.  autopsy/core/interceptor.py         (deps: events, tracer)
4.  autopsy/core/decorator.py           (deps: events, tracer, interceptor)
5.  autopsy/core/replay.py              (deps: events, tracer)
6.  autopsy/diagnostics/prompts.py      (no deps)
7.  autopsy/diagnostics/gmi_agent.py    (deps: events, prompts)
8.  autopsy/diagnostics/gemini_agent.py (deps: events, prompts)
9.  autopsy/server/ws_manager.py        (no deps)
10. autopsy/server/routes/traces.py     (deps: tracer, events)
11. autopsy/server/routes/diagnose.py   (deps: gmi_agent, gemini_agent)
12. autopsy/server/routes/replay.py     (deps: replay, tracer)
13. autopsy/server/app.py               (deps: all routes, ws_manager)
14. autopsy/deploy/rocketride.py        (no deps)
15. autopsy/cli/main.py                 (deps: all of above)
16. autopsy/__init__.py                 (exports lens, LensConfig)
17. pyproject.toml
18. dashboard/package.json + vite.config.ts
19. dashboard/src/types.ts
20. dashboard/src/hooks/useTraceSocket.ts
21. dashboard/src/hooks/useTraceStore.ts
22. dashboard/src/components/DAGGraph.tsx
23. dashboard/src/components/NodeDetail.tsx
24. dashboard/src/components/DiagnosticsPanel.tsx
25. dashboard/src/components/ReplayControls.tsx
26. dashboard/src/components/LatencyChart.tsx
27. dashboard/src/components/TraceList.tsx
28. dashboard/src/App.tsx
29. examples/broken_agent.py
30. scripts/build_dashboard.sh
31. tests/ (unit then integration)
32. README.md
```

---

## 20. `autopsy/__init__.py` — Public API

```python
"""
Public API — this is the ONLY thing users import.
"""
from autopsy.core.decorator import LensDecorator
from autopsy.core.events import TraceBundle, DiagnosisResult

class LensConfig:
    def __init__(
        self,
        gmi_api_key: str = None,
        google_ai_api_key: str = None,
        session_dir: str = None,
        port: int = 7823,
        auto_diagnose: bool = False,   # auto-diagnose on any error node
        model: str = None,             # override default GMI model
    ): ...

# Default instance — works with zero config (reads from env)
lens = LensDecorator()

# Named export for config
__all__ = ["lens", "LensConfig", "TraceBundle"]
```

---

## 21. README.md — Key Sections

````markdown
# autopsy

> _Your agent died. Here's why._
> One decorator. Full agent visibility. AI that diagnoses your AI.

## Install

```bash
pip install autopsy
```

## Quickstart

```python
from autopsy import lens

@lens.trace
async def my_agent(query: str):
    # your existing agent code — zero changes
    ...
```

```bash
autopsy run agent.py
# Browser opens at http://localhost:7823
```

## What you get

- **Live DAG** of every agent hop, tool call, and LLM completion
- **Root cause diagnosis** via GMI Cloud H100 inference in <2s
- **Time-travel replay** — re-run from any node with any model
- **Latency breakdown** — see exactly where your agent is slow
- **Works with everything** — OpenAI, LangChain, AutoGen, raw httpx

## Deploy for your team

```bash
autopsy deploy --name "my-project-traces"
# Returns: https://your-agent.rocketride.io
```

## Environment variables

Copy `.env.example` to `.env` and fill in your keys.
````

---

## 22. Critical Implementation Notes for AI IDE

1. **The interceptor is the hardest part.** It must not break existing code. Test it against bare `openai` calls, LangChain chains, and raw `httpx` requests before building anything else.

2. **ContextVars are essential.** Without them, nested async calls will mix up their node IDs. Every event emission must read `_current_node_id.get()` and `_call_depth.get()` from the contextvar, not from a global.

3. **The WebSocket broadcast must be non-blocking.** The agent run must not slow down because the dashboard is slow. Use `asyncio.create_task(ws_manager.broadcast(...))` — fire and forget.

4. **React Flow node layout.** Use the `dagre` library for auto-layout. Install `@dagrejs/dagre` and `reactflow`. Compute layout in a `useMemo` whenever `dag_edges` changes.

5. **The broken_agent.py demo must be deterministic.** The failure must happen every time. Use a hardcoded oversized context string rather than relying on real API behavior.

6. **Session persistence.** Write `sessions_index.json` atomically (write to `.tmp` then rename). Concurrent runs must not corrupt the index.

7. **GMI API key is required for the demo to work.** Add a clear error message if `GMI_API_KEY` is not set: `"Set GMI_API_KEY in .env — get yours at gmi.ai/console"`.

8. **Bundle the React build.** Run `scripts/build_dashboard.sh` and commit the `autopsy/server/static/` output so users don't need Node.js to use the package.

9. **Token cost estimation.** Use `tiktoken` for OpenAI models only (`gpt-*`, `o1-*`, `o3-*`). For all other models (LLaMA, Gemini, DeepSeek), use the universal char-based fallback: `estimate = total_chars // 4`. Always catch `tiktoken` exceptions and fall back silently — never let token estimation crash the trace.

10. **The `@lens.trace` decorator must handle both `async def` and regular `def`.** Wrap sync functions with `asyncio.get_event_loop().run_in_executor` and still emit events.

11. **All server config env vars use the `AUTOPSY_` prefix** (e.g. `AUTOPSY_PORT`, `AUTOPSY_HOST`, `AUTOPSY_DEBUG`). Do NOT use the old `ROCKETLENS_` prefix anywhere in code or docs — it was a previous project name and will confuse users.

12. **Test coverage requirements.** Each test file must include at minimum:

`tests/unit/test_events.py`:

- All event dataclasses serialize/deserialize to JSON without loss
- TraceBundle round-trips through `asdict()` + `json.dumps()` + `json.loads()`

`tests/unit/test_decorator.py`:

- `@lens.trace` on an async function emits NodeStartEvent + NodeEndEvent
- Nested `@lens.trace` calls produce correct parent_node_id linkage
- Exception in traced function emits NodeErrorEvent and re-raises
- `get_current_session()` returns None after session finalize
- Use `respx` to mock httpx and avoid real API calls

`tests/unit/test_replay.py`:

- `replay_from_node()` returns frozen output for pre-checkpoint nodes
- `replay_from_node()` calls real function for the target node
- `compare()` produces correct latency_delta and token_delta
- Side effect warning is printed to stdout before replay

`tests/integration/test_server.py`:

- `GET /api/sessions` returns empty list when no sessions
- `POST /api/sessions/{id}/diagnose` returns 404 for unknown session
- WebSocket `/ws/live` receives session_complete with summary (not full bundle)
- Use `httpx.AsyncClient(app=app, base_url="http://test")` for in-process testing

`tests/integration/test_diagnostics.py`:

- `GMIAgent.diagnose()` returns a valid DiagnosisResult when given a bundle with errors
- Falls back to GeminiAgent when bundle token estimate > 32k
- Mock both GMI and Gemini API responses with `respx`
