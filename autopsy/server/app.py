"""FastAPI server: REST + WebSocket for the autopsy dashboard.

All routes are tolerant of partial / missing data so the demo never crashes.
If the React build is not present in autopsy/server/static, the server serves
a built-in single-file fallback dashboard (in static_fallback.py) that still
visualises the trace.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from autopsy.core.replay import ReplayEngine
from autopsy.core.tracer import _default_session_dir, list_sessions, load_bundle

from .static_fallback import FALLBACK_HTML, FALLBACK_JS
from .ws_manager import ws_manager

logger = logging.getLogger("autopsy.server")

VERSION = "0.1.0"


def _session_dir() -> Path:
    return _default_session_dir()


class DiagnoseRequest(BaseModel):
    node_id: Optional[str] = None
    force_model: Optional[str] = None  # "gmi" | "gemini" | None (auto)


class ReplayRequest(BaseModel):
    node_id: str
    model_override: Optional[str] = None
    prompt_patch: Optional[str] = None
    fix_description: Optional[str] = "Fix applied"
    live: bool = False


def create_app() -> FastAPI:
    app = FastAPI(title="autopsy", version=VERSION)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Clear any stale fix marker from a previous run so each session starts
    # in the unfixed (broken) state. The user must explicitly click
    # "Apply fix & replay" on the dashboard to set the marker.
    for _marker in (Path.home() / ".autopsy" / "fix_applied",
                    Path.cwd() / ".autopsy" / "fix_applied"):
        try:
            if _marker.exists():
                _marker.unlink()
        except Exception:
            pass

    # --- REST endpoints --------------------------------------------------------

    @app.get("/health")
    async def health() -> dict:
        sess_dir = _session_dir()
        return {
            "status": "ok",
            "version": VERSION,
            "session_dir": str(sess_dir),
            "sessions_count": len(list_sessions(sess_dir)),
        }

    @app.get("/api/sessions")
    async def get_sessions() -> JSONResponse:
        try:
            sessions = list_sessions(_session_dir())
        except Exception:
            logger.exception("get_sessions failed")
            sessions = []
        return JSONResponse(sessions)

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str) -> JSONResponse:
        bundle = load_bundle(_session_dir(), session_id)
        if bundle is None:
            raise HTTPException(status_code=404, detail="session not found")
        return JSONResponse({
            "session_id": bundle.session_id,
            "created_at": bundle.created_at,
            "agent_name": bundle.agent_name,
            "input_query": bundle.input_query,
            "agent_module_path": bundle.agent_module_path,
            "agent_fn_name": bundle.agent_fn_name,
            "events": bundle.events,
            "dag_edges": bundle.dag_edges,
            "node_index": bundle.node_index,
            "summary": bundle.summary,
        })

    @app.get("/api/sessions/{session_id}/dag")
    async def get_session_dag(session_id: str) -> JSONResponse:
        bundle = load_bundle(_session_dir(), session_id)
        if bundle is None:
            raise HTTPException(status_code=404, detail="session not found")
        # Compact node index for DAG rendering.
        nodes = []
        for nid, ni in bundle.node_index.items():
            start = ni.get("start_event") or {}
            end = ni.get("end_event") or {}
            err = ni.get("error_event") or {}
            nodes.append({
                "node_id": nid,
                "node_name": start.get("node_name", ""),
                "node_type": start.get("node_type", ""),
                "parent_node_id": start.get("parent_node_id"),
                "depth": start.get("depth", 0),
                "duration_ms": end.get("duration_ms") or err.get("duration_ms") or 0,
                "status": "error" if err else ("ok" if end else "running"),
            })
        return JSONResponse({
            "session_id": bundle.session_id,
            "nodes": nodes,
            "edges": bundle.dag_edges,
            "summary": bundle.summary,
        })

    @app.post("/api/sessions/{session_id}/diagnose")
    async def diagnose(session_id: str, req: DiagnoseRequest) -> JSONResponse:
        bundle = load_bundle(_session_dir(), session_id)
        if bundle is None:
            raise HTTPException(status_code=404, detail="session not found")
        try:
            from autopsy.diagnostics.gemini_agent import (
                GeminiAgent,
                estimate_bundle_tokens,
            )
            from autopsy.diagnostics.gmi_agent import GMIAgent
            from autopsy.diagnostics.rocketride_agent import RocketRideAgent

            # OPTIONAL pre-processing via RocketRide pipeline. When the engine
            # is reachable, this scrubs PII, summarizes the trace, and pulls
            # similar past failures from vector memory. The result is attached
            # to the bundle so the GMI/Gemini prompt has richer context.
            # When the engine isn't running we skip silently and behave
            # exactly as before -> no breakage.
            rr_meta: dict = {"used": False}
            try:
                rr = RocketRideAgent()
                if rr.available():
                    rr_ctx = await rr.preprocess(bundle)
                    if rr_ctx:
                        bundle.rocketride_context = rr_ctx  # type: ignore
                        rr_meta = {
                            "used": True,
                            "pipeline": rr_ctx.get("_rocketride", {}).get(
                                "pipeline", "autopsy_diagnose.pipe"),
                            "similar_cases": len(
                                rr_ctx.get("similar_cases", []) or []),
                        }
            except Exception:
                logger.exception(
                    "RocketRide pre-processing failed; "
                    "falling back to direct LLM diagnosis")

            force = (req.force_model or "").lower()
            if force == "gemini":
                agent = GeminiAgent()
            elif force == "gmi":
                agent = GMIAgent()
            else:
                est = estimate_bundle_tokens(bundle)
                agent = GeminiAgent() if est > 32_000 else GMIAgent()
            result = await agent.diagnose(bundle, req.node_id)
            # Attach RocketRide metadata so the dashboard can show it.
            payload = asdict(result)
            payload["rocketride"] = rr_meta
            return JSONResponse(payload)
        except Exception:
            logger.exception("diagnose route failed")
            raise HTTPException(status_code=500, detail="diagnose failed")

    @app.get("/api/rocketride/status")
    async def rocketride_status() -> JSONResponse:
        """Health-check the RocketRide integration for the dashboard pill."""
        try:
            from autopsy.diagnostics.rocketride_agent import RocketRideAgent
            agent = RocketRideAgent()
            pre = await agent.preflight()
            return JSONResponse({
                "sdk_installed": pre.sdk_installed,
                "engine_reachable": pre.engine_reachable,
                "pipe_file_present": pre.pipe_file_present,
                "ok": pre.ok,
                "detail": pre.detail,
                "uri": agent.uri,
                "pipe_path": str(agent.pipe_path),
            })
        except Exception as e:
            return JSONResponse({
                "sdk_installed": False,
                "engine_reachable": False,
                "pipe_file_present": False,
                "ok": False,
                "detail": str(e),
            })

    @app.post("/api/sessions/{session_id}/replay")
    async def replay(session_id: str, req: ReplayRequest) -> JSONResponse:
        bundle = load_bundle(_session_dir(), session_id)
        if bundle is None:
            raise HTTPException(status_code=404, detail="session not found")
        engine = ReplayEngine(bundle)
        if req.live:
            try:
                out = await engine.live_replay_from_node(
                    req.node_id, req.model_override, req.prompt_patch)
                return JSONResponse(out)
            except Exception as exc:
                logger.exception("live replay failed - falling back to simulated")
                sim = engine.simulated_replay(
                    req.node_id, req.fix_description or "Fix applied")
                sim["live_replay_error"] = str(exc)
                return JSONResponse(sim)
        sim = engine.simulated_replay(
            req.node_id, req.fix_description or "Fix applied")
        return JSONResponse(sim)

    @app.delete("/api/sessions")
    async def delete_all_sessions(keep_live: int = 0) -> JSONResponse:
        """Bulk-delete sessions to clean up noisy sidebars in demos.

        Query params:
          keep_live=1 -> preserve sessions whose status is "running"
        """
        sd = _session_dir()
        deleted = 0
        for p in sd.glob("*.json"):
            if p.name == "sessions_index.json":
                continue
            try:
                if keep_live:
                    try:
                        bundle = json.loads(p.read_text())
                        if (bundle.get("summary") or {}).get("status") == "running":
                            continue
                    except Exception:
                        pass
                p.unlink()
                deleted += 1
            except Exception:
                logger.exception("delete failed for %s", p)
        # Rebuild index from remaining files (simpler than surgical updates).
        index_path = sd / "sessions_index.json"
        try:
            remaining = []
            for p in sd.glob("*.json"):
                if p.name == "sessions_index.json":
                    continue
                try:
                    b = json.loads(p.read_text())
                    remaining.append({
                        "session_id": b.get("session_id"),
                        "agent_name": b.get("agent_name"),
                        "created_at": (b.get("events") or [{}])[0].get("timestamp", 0),
                        "status": (b.get("summary") or {}).get("status", "unknown"),
                        "error_count": (b.get("summary") or {}).get("error_count", 0),
                        "node_count": (b.get("summary") or {}).get("node_count", 0),
                        "input_query": b.get("input_query", ""),
                    })
                except Exception:
                    pass
            index_path.write_text(json.dumps(remaining))
        except Exception:
            logger.exception("rebuilding session index failed")
        return JSONResponse({"deleted": deleted})

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str) -> JSONResponse:
        sd = _session_dir()
        path = sd / f"{session_id}.json"
        existed = path.exists()
        try:
            if existed:
                path.unlink()
            # Update the index.
            index_path = sd / "sessions_index.json"
            if index_path.exists():
                try:
                    data = json.loads(index_path.read_text())
                    if isinstance(data, list):
                        data = [e for e in data if e.get("session_id") != session_id]
                        index_path.write_text(json.dumps(data))
                except Exception:
                    pass
        except Exception:
            logger.exception("delete_session failed")
        return JSONResponse({"deleted": existed})

    # --- Demo control endpoints (used by the live-loop demo) -----------------

    def _fix_markers() -> list[Path]:
        """Locations the demo pipeline watches for the fix-applied signal."""
        home = Path.home() / ".autopsy" / "fix_applied"
        cwd = Path.cwd() / ".autopsy" / "fix_applied"
        return [home, cwd]

    def _write_fix_markers() -> list[str]:
        """Write the fix marker to every watched location.

        Returns the list of paths that were written successfully. Failures are
        swallowed (e.g. sandboxed home dir).
        """
        written: list[str] = []
        for p in _fix_markers():
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("fix applied by dashboard")
                written.append(str(p))
            except (PermissionError, OSError):
                logger.debug("could not write fix marker %s", p)
            except Exception:
                logger.exception("unexpected error writing fix marker %s", p)
        return written

    @app.post("/api/demo/fix")
    async def demo_apply_fix() -> JSONResponse:
        """Marks the live demo pipeline as 'fixed'.

        After this call, the next iteration of the broken pipeline will run
        in success mode (truncates oversized briefs before the synthesizer).
        """
        markers_written = _write_fix_markers()
        await ws_manager.broadcast({
            "type": "demo_status",
            "data": {"fix_applied": True, "markers": markers_written},
        })
        return JSONResponse({"fix_applied": True, "markers": markers_written})

    @app.post("/api/demo/reset")
    async def demo_reset() -> JSONResponse:
        """Removes the fix marker so the demo loop goes back to failing."""
        removed = []
        for p in _fix_markers():
            try:
                if p.exists():
                    p.unlink()
                    removed.append(str(p))
            except Exception:
                logger.exception("could not remove fix marker %s", p)
        await ws_manager.broadcast({
            "type": "demo_status",
            "data": {"fix_applied": False, "removed": removed},
        })
        return JSONResponse({"fix_applied": False, "removed": removed})

    @app.get("/api/demo/status")
    async def demo_status() -> JSONResponse:
        applied = any(p.exists() for p in _fix_markers())
        return JSONResponse({"fix_applied": applied})

    # --- WebSocket -------------------------------------------------------------

    @app.websocket("/ws/live")
    async def ws_live(ws: WebSocket):
        await ws_manager.connect(ws)
        try:
            # Send initial sessions list.
            try:
                await ws.send_text(json.dumps({
                    "type": "sessions_list",
                    "data": list_sessions(_session_dir()),
                }))
            except Exception:
                pass
            while True:
                # Keep the socket open; we don't expect client messages but
                # consume them anyway so the read pump doesn't stall.
                try:
                    await ws.receive_text()
                except WebSocketDisconnect:
                    break
                except Exception:
                    break
        finally:
            await ws_manager.disconnect(ws)

    # --- Static files / fallback HTML ------------------------------------------

    static_dir = Path(__file__).parent / "static"
    has_static_build = (static_dir / "index.html").exists()

    if has_static_build:
        # Serve built React app
        app.mount("/assets", StaticFiles(directory=str(static_dir / "assets")),
                  name="assets")

        @app.get("/_dashboard.js")
        async def _fallback_js_when_static():
            from fastapi.responses import Response
            return Response(content=FALLBACK_JS, media_type="application/javascript")

        @app.get("/", response_class=HTMLResponse)
        async def root_index() -> HTMLResponse:
            return HTMLResponse((static_dir / "index.html").read_text())

        @app.get("/{full_path:path}", response_class=HTMLResponse)
        async def catch_all(full_path: str, request: Request) -> HTMLResponse:
            if full_path.startswith(("api/", "ws/", "assets/", "health", "_dashboard")):
                raise HTTPException(status_code=404)
            return HTMLResponse((static_dir / "index.html").read_text())
    else:
        @app.get("/_dashboard.js")
        async def fallback_js():
            from fastapi.responses import Response
            return Response(content=FALLBACK_JS, media_type="application/javascript")

        @app.get("/", response_class=HTMLResponse)
        async def root_fallback() -> HTMLResponse:
            return HTMLResponse(FALLBACK_HTML)

        @app.get("/{full_path:path}", response_class=HTMLResponse)
        async def catch_all(full_path: str) -> HTMLResponse:
            if full_path.startswith(("api/", "ws/", "health", "_dashboard")):
                raise HTTPException(status_code=404)
            return HTMLResponse(FALLBACK_HTML)

    # Wire the lens decorator to the ws_manager so live broadcasts work.
    try:
        from autopsy import lens
        lens.set_ws_manager(ws_manager)
    except Exception:
        logger.exception("autopsy: failed to attach ws_manager to lens")

    return app


# Module-level app for `uvicorn autopsy.server.app:app` usage.
app = create_app()
