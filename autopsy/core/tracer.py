"""TraceSession - manages the lifetime of one agent run.

Responsibilities:
1. Hold an asyncio.Queue of events emitted by the decorator and interceptors.
2. Run a background drain task that:
   - appends to self.events
   - broadcasts each event to the WSManager
   - builds dag_edges and node_index
3. finalize() computes summary, persists TraceBundle to disk, broadcasts session_complete.
4. install_interceptors() / uninstall_interceptors() activate/deactivate the OpenAI+httpx
   monkey-patches for the lifetime of this session.

The implementation is intentionally defensive: a bug in tracing must never crash
the user's agent. Every callback path catches Exception and logs to stderr.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

try:
    from filelock import FileLock
    _HAS_FILELOCK = True
except Exception:
    _HAS_FILELOCK = False

from .events import (
    BaseEvent,
    SessionEndEvent,
    SessionStartEvent,
    TraceBundle,
)

logger = logging.getLogger("autopsy.tracer")

_current_session: contextvars.ContextVar[Optional["TraceSession"]] = contextvars.ContextVar(
    "_current_session", default=None
)


def get_current_session() -> Optional["TraceSession"]:
    return _current_session.get()


def set_current_session(session: Optional["TraceSession"]) -> None:
    _current_session.set(session)


def _default_session_dir() -> Path:
    """Pick a writable session directory.

    Order of preference:
      1. AUTOPSY_SESSION_DIR env var (must be writable; created on demand)
      2. ~/.autopsy/sessions (typical user install)
      3. ./.autopsy/sessions (sandbox / read-only home / CI)
      4. /tmp/autopsy_sessions (last resort)
    """
    candidates: list[Path] = []
    raw = os.environ.get("AUTOPSY_SESSION_DIR")
    if raw:
        candidates.append(Path(os.path.expanduser(raw)))
    candidates.append(Path(os.path.expanduser("~/.autopsy/sessions")))
    candidates.append(Path.cwd() / ".autopsy" / "sessions")
    import tempfile as _tmp
    candidates.append(Path(_tmp.gettempdir()) / "autopsy_sessions")
    for c in candidates:
        try:
            c.mkdir(parents=True, exist_ok=True)
            # confirm writable
            probe = c / ".write_probe"
            probe.write_text("")
            probe.unlink(missing_ok=True)
            return c
        except Exception:
            continue
    return candidates[-1]


def _safe_json_default(obj: Any) -> Any:
    """JSON default that never raises - falls back to repr()."""
    try:
        return str(obj)
    except Exception:
        return "<unserializable>"


def _safe_asdict(event: BaseEvent) -> dict:
    """Convert an event to a JSON-safe dict. Never raises."""
    try:
        d = asdict(event)
    except Exception:
        d = {"event_type": getattr(event, "event_type", "unknown"),
             "error": "asdict_failed"}
    try:
        json.dumps(d, default=_safe_json_default)
    except Exception:
        d = {k: str(v) for k, v in d.items()}
    return d


class TraceSession:
    """Holds state for a single end-to-end agent run.

    A session is created by the @lens.trace decorator on the *top-level* call
    and accessed via ContextVars by nested calls and interceptors.
    """

    def __init__(
        self,
        agent_name: str = "agent",
        input_query: str = "",
        agent_module_path: str = "",
        agent_fn_name: str = "",
        session_dir: Optional[Path] = None,
        ws_manager: Optional[Any] = None,
    ):
        self.session_id: str = str(uuid4())
        self.created_at: float = time.time()
        self.agent_name: str = agent_name
        self.input_query: str = input_query
        self.agent_module_path: str = agent_module_path
        self.agent_fn_name: str = agent_fn_name
        self.session_dir: Path = session_dir or _default_session_dir()

        self.events: list[dict] = []
        self.dag_edges: list[list[str]] = []  # [[parent_id, child_id], ...]
        self.node_index: dict[str, dict] = {}
        self.replay_checkpoints: dict[str, str] = {}

        self._queue: asyncio.Queue[Optional[BaseEvent]] = asyncio.Queue()
        self._drain_task: Optional[asyncio.Task] = None
        self._finalized: bool = False
        self._start_perf: float = time.perf_counter()

        # set by app at runtime if available
        self.ws_manager = ws_manager
        self._interceptor_installed: bool = False

        self._emit_session_start()
        self._start_drain()

    def _emit_session_start(self) -> None:
        ev = SessionStartEvent(
            session_id=self.session_id,
            agent_name=self.agent_name,
            input_query=self.input_query[:2000],
            metadata={"agent_module_path": self.agent_module_path,
                      "agent_fn_name": self.agent_fn_name},
        )
        # Synchronously stash so it's present even before the drainer runs.
        self._record_event_sync(ev)

    def _start_drain(self) -> None:
        try:
            loop = asyncio.get_running_loop()
            self._drain_task = loop.create_task(self._drain_loop())
        except RuntimeError:
            # No running loop yet (e.g. invoked from sync entry). Drain lazily.
            self._drain_task = None

    async def _drain_loop(self) -> None:
        while True:
            try:
                ev = await self._queue.get()
            except Exception:
                return
            if ev is None:
                return
            try:
                self._record_event_sync(ev)
                await self._broadcast_event(ev)
            except Exception:
                logger.exception("autopsy: drain failed for event")

    def _record_event_sync(self, ev: BaseEvent) -> None:
        """Append event to in-memory store and update derived indexes."""
        try:
            d = _safe_asdict(ev)
            self.events.append(d)

            etype = d.get("event_type")
            node_id = d.get("node_id")

            if etype == "node_start" and node_id:
                self.node_index.setdefault(node_id, {})
                self.node_index[node_id]["start_event"] = d
                parent_id = d.get("parent_node_id")
                if parent_id:
                    self.dag_edges.append([parent_id, node_id])
            elif etype == "node_end" and node_id:
                self.node_index.setdefault(node_id, {})
                self.node_index[node_id]["end_event"] = d
                out = d.get("output_data")
                try:
                    self.replay_checkpoints[node_id] = json.dumps(
                        out, default=_safe_json_default
                    )
                except Exception:
                    self.replay_checkpoints[node_id] = json.dumps(
                        {"__unserializable": True})
            elif etype == "node_error" and node_id:
                self.node_index.setdefault(node_id, {})
                self.node_index[node_id]["error_event"] = d
            elif etype == "llm_request" and node_id:
                self.node_index.setdefault(node_id, {})
                self.node_index[node_id].setdefault("llm_events", []).append(d)
            elif etype == "llm_response" and node_id:
                self.node_index.setdefault(node_id, {})
                self.node_index[node_id].setdefault("llm_events", []).append(d)
            elif etype in ("tool_call", "tool_result") and node_id:
                self.node_index.setdefault(node_id, {})
                self.node_index[node_id].setdefault("tool_events", []).append(d)
        except Exception:
            logger.exception("autopsy: _record_event_sync failed")

    async def _broadcast_event(self, ev: BaseEvent) -> None:
        if self.ws_manager is None:
            return
        try:
            await self.ws_manager.broadcast_event(ev)
        except Exception:
            logger.exception("autopsy: ws broadcast failed")

    async def emit(self, ev: BaseEvent) -> None:
        """Called by decorator/interceptor. Never raises."""
        try:
            ev.session_id = self.session_id
        except Exception:
            pass
        # Ensure a drain task is running for this loop.
        if self._drain_task is None or self._drain_task.done():
            try:
                self._drain_task = asyncio.get_running_loop().create_task(
                    self._drain_loop())
            except Exception:
                # Fallback: record synchronously and broadcast best-effort.
                self._record_event_sync(ev)
                try:
                    await self._broadcast_event(ev)
                except Exception:
                    pass
                return
        try:
            self._queue.put_nowait(ev)
        except Exception:
            self._record_event_sync(ev)

    def emit_sync(self, ev: BaseEvent) -> None:
        """Synchronous emit for code paths outside an event loop."""
        try:
            ev.session_id = self.session_id
        except Exception:
            pass
        self._record_event_sync(ev)

    def install_interceptors(self) -> None:
        if self._interceptor_installed:
            return
        try:
            from .interceptor import InterceptorManager
            InterceptorManager.install(self)
            self._interceptor_installed = True
        except Exception:
            logger.exception("autopsy: interceptor install failed")

    def uninstall_interceptors(self) -> None:
        if not self._interceptor_installed:
            return
        try:
            from .interceptor import InterceptorManager
            InterceptorManager.uninstall(self)
        except Exception:
            logger.exception("autopsy: interceptor uninstall failed")
        finally:
            self._interceptor_installed = False

    async def finalize(self, status: str = "success") -> dict:
        """Drain remaining events, compute summary, persist bundle, broadcast."""
        if self._finalized:
            return self.build_bundle().__dict__
        self._finalized = True

        # Signal drainer to stop and wait briefly.
        try:
            self._queue.put_nowait(None)
        except Exception:
            pass
        if self._drain_task is not None:
            try:
                await asyncio.wait_for(self._drain_task, timeout=2.0)
            except Exception:
                pass

        # Compute summary.
        total_duration_ms = (time.perf_counter() - self._start_perf) * 1000
        node_count = sum(1 for e in self.events if e.get("event_type") == "node_start")
        error_count = sum(1 for e in self.events if e.get("event_type") == "node_error")
        total_tokens = 0
        for e in self.events:
            if e.get("event_type") == "llm_response":
                total_tokens += int(e.get("total_tokens") or 0)
        final_status = status
        if status == "success" and error_count > 0:
            final_status = "error"

        # Emit final SessionEndEvent.
        session_end = SessionEndEvent(
            session_id=self.session_id,
            total_duration_ms=total_duration_ms,
            total_tokens=total_tokens,
            total_cost_usd=0.0,
            node_count=node_count,
            error_count=error_count,
            status=final_status,
        )
        self._record_event_sync(session_end)
        await self._broadcast_event(session_end)

        bundle = self.build_bundle()
        bundle.summary = {
            "total_duration_ms": total_duration_ms,
            "total_tokens": total_tokens,
            "node_count": node_count,
            "error_count": error_count,
            "status": final_status,
            "error_nodes": [
                e.get("node_id") for e in self.events
                if e.get("event_type") == "node_error"
            ],
        }

        # Persist to disk.
        try:
            self._persist_bundle(bundle)
        except Exception:
            logger.exception("autopsy: persist failed")

        # Broadcast session_complete (summary only).
        if self.ws_manager is not None:
            try:
                await self.ws_manager.broadcast({
                    "type": "session_complete",
                    "data": {
                        "session_id": self.session_id,
                        "agent_name": self.agent_name,
                        "summary": bundle.summary,
                        "created_at": self.created_at,
                    },
                })
            except Exception:
                logger.exception("autopsy: session_complete broadcast failed")

        return bundle.__dict__

    def build_bundle(self) -> TraceBundle:
        return TraceBundle(
            session_id=self.session_id,
            created_at=self.created_at,
            agent_name=self.agent_name,
            input_query=self.input_query,
            agent_module_path=self.agent_module_path,
            agent_fn_name=self.agent_fn_name,
            events=list(self.events),
            dag_edges=list(self.dag_edges),
            node_index=dict(self.node_index),
            replay_checkpoints=dict(self.replay_checkpoints),
            summary={},
        )

    def _persist_bundle(self, bundle: TraceBundle) -> None:
        """Write bundle to ~/.autopsy/sessions/{session_id}.json + update index atomically."""
        self.session_dir.mkdir(parents=True, exist_ok=True)
        bundle_path = self.session_dir / f"{self.session_id}.json"
        tmp_path = bundle_path.with_suffix(".json.tmp")

        payload = {
            "session_id": bundle.session_id,
            "created_at": bundle.created_at,
            "agent_name": bundle.agent_name,
            "input_query": bundle.input_query,
            "agent_module_path": bundle.agent_module_path,
            "agent_fn_name": bundle.agent_fn_name,
            "events": bundle.events,
            "dag_edges": bundle.dag_edges,
            "node_index": bundle.node_index,
            "replay_checkpoints": bundle.replay_checkpoints,
            "summary": bundle.summary,
        }
        with open(tmp_path, "w") as f:
            json.dump(payload, f, default=_safe_json_default)
        os.replace(tmp_path, bundle_path)

        # Update index atomically.
        index_path = self.session_dir / "sessions_index.json"
        lock_path = self.session_dir / "sessions_index.lock"

        def _update_index() -> None:
            existing: list[dict] = []
            if index_path.exists():
                try:
                    with open(index_path) as f:
                        existing = json.load(f)
                        if not isinstance(existing, list):
                            existing = []
                except Exception:
                    existing = []
            summary_entry = {
                "session_id": bundle.session_id,
                "agent_name": bundle.agent_name,
                "created_at": bundle.created_at,
                "status": bundle.summary.get("status", "unknown"),
                "error_count": bundle.summary.get("error_count", 0),
                "total_tokens": bundle.summary.get("total_tokens", 0),
                "node_count": bundle.summary.get("node_count", 0),
                "total_duration_ms": bundle.summary.get("total_duration_ms", 0),
                "input_query": bundle.input_query[:200],
            }
            # Dedupe by session_id.
            existing = [e for e in existing if e.get("session_id") != bundle.session_id]
            existing.append(summary_entry)
            existing.sort(key=lambda e: e.get("created_at", 0), reverse=True)
            index_tmp = index_path.with_suffix(".json.tmp")
            with open(index_tmp, "w") as f:
                json.dump(existing, f, default=_safe_json_default)
            os.replace(index_tmp, index_path)

        if _HAS_FILELOCK:
            try:
                with FileLock(str(lock_path), timeout=5):
                    _update_index()
            except Exception:
                logger.exception("autopsy: lock acquire failed, updating without lock")
                _update_index()
        else:
            _update_index()


def load_bundle(session_dir: Path, session_id: str) -> Optional[TraceBundle]:
    """Load a TraceBundle from disk by session_id."""
    path = session_dir / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        return TraceBundle(
            session_id=data.get("session_id", ""),
            created_at=data.get("created_at", 0.0),
            agent_name=data.get("agent_name", ""),
            input_query=data.get("input_query", ""),
            agent_module_path=data.get("agent_module_path", ""),
            agent_fn_name=data.get("agent_fn_name", ""),
            events=data.get("events", []),
            dag_edges=data.get("dag_edges", []),
            node_index=data.get("node_index", {}),
            replay_checkpoints=data.get("replay_checkpoints", {}),
            summary=data.get("summary", {}),
        )
    except Exception:
        logger.exception("autopsy: load_bundle failed for %s", session_id)
        return None


def list_sessions(session_dir: Optional[Path] = None) -> list[dict]:
    """Return the sessions_index.json contents (or empty list)."""
    d = session_dir or _default_session_dir()
    p = d / "sessions_index.json"
    if not p.exists():
        return []
    try:
        with open(p) as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []
