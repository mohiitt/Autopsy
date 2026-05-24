"""Integration tests for FastAPI server using httpx in-process."""

import httpx
import pytest

from autopsy import lens
from autopsy.core.tracer import list_sessions, _default_session_dir
from autopsy.server.app import create_app


@pytest.mark.asyncio
async def test_health_endpoint():
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                  base_url="http://test") as c:
        r = await c.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert "version" in data


@pytest.mark.asyncio
async def test_sessions_empty_initially():
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                  base_url="http://test") as c:
        r = await c.get("/api/sessions")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


@pytest.mark.asyncio
async def test_unknown_session_returns_404():
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                  base_url="http://test") as c:
        r = await c.get("/api/sessions/nonexistent")
        assert r.status_code == 404
        r = await c.post("/api/sessions/nonexistent/diagnose", json={})
        assert r.status_code == 404
        r = await c.post("/api/sessions/nonexistent/replay",
                         json={"node_id": "x"})
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_full_diagnose_replay_flow():
    @lens.trace(name="bad")
    async def bad():
        raise ValueError("oops")

    with pytest.raises(ValueError):
        await bad()

    sid = list_sessions(_default_session_dir())[0]["session_id"]

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                  base_url="http://test") as c:
        r = await c.get(f"/api/sessions/{sid}")
        assert r.status_code == 200
        bundle = r.json()
        assert bundle["session_id"] == sid

        r = await c.get(f"/api/sessions/{sid}/dag")
        assert r.status_code == 200
        assert "nodes" in r.json()

        # Diagnose with no API key -> heuristic fallback (must not crash).
        r = await c.post(f"/api/sessions/{sid}/diagnose", json={})
        assert r.status_code == 200
        diag = r.json()
        assert "root_cause" in diag

        # Find a node id to replay.
        bundle_resp = bundle
        first_node = next(iter(bundle_resp["node_index"].keys()))
        r = await c.post(f"/api/sessions/{sid}/replay",
                          json={"node_id": first_node, "fix_description": "fix"})
        assert r.status_code == 200
        result = r.json()
        assert result["summary"]["status"] == "success"
