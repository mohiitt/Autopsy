"""
Tests for the optional RocketRide diagnostics integration.

These tests exercise the graceful-degradation paths because the test
environment doesn't (and shouldn't need to) run a RocketRide engine.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from autopsy.diagnostics.rocketride_agent import (
    DEFAULT_PIPE_PATH,
    RocketRideAgent,
    RocketRidePreflight,
)


def test_default_pipe_file_exists_in_repo() -> None:
    """The shipped pipeline file must exist and conform to the real
    RocketRide schema so the VS Code extension can render it.
    """
    import json
    assert DEFAULT_PIPE_PATH.exists(), (
        f"shipped pipeline missing: {DEFAULT_PIPE_PATH}")
    data = json.loads(DEFAULT_PIPE_PATH.read_text())

    # Real RocketRide pipe-file schema (per the VS Code extension docs):
    #   - top-level keys: components (first), project_id, viewport, version
    #   - components: list of {id, provider, config, [input], [control], [ui]}
    keys = list(data.keys())
    assert keys[0] == "components", "components must be the first top-level key"
    for required in ("project_id", "viewport", "version"):
        assert required in data, f"missing required top-level field: {required}"

    components = data["components"]
    assert isinstance(components, list) and len(components) > 0

    ids = {c["id"] for c in components}
    for c in components:
        assert "id" in c and "provider" in c and "config" in c
        # all input/control refs must point to existing components
        for inp in c.get("input") or []:
            assert inp["from"] in ids, (
                f"input ref points to missing component: {inp['from']}")
        for ctrl in c.get("control") or []:
            assert ctrl["from"] in ids, (
                f"control ref points to missing component: {ctrl['from']}")


def test_agent_available_reflects_state() -> None:
    """`available()` is True iff SDK installed AND pipe file present."""
    agent = RocketRideAgent()
    # Whether True or False, it should match this exact rule.
    from autopsy.diagnostics import rocketride_agent as ra
    expected = ra._SDK_AVAILABLE and agent.pipe_path.exists()
    assert agent.available() is expected


def test_agent_uses_missing_pipe_path_gracefully(tmp_path: Path) -> None:
    """Pointing the agent at a non-existent .pipe must not blow up."""
    bogus = tmp_path / "no_such.pipe"
    agent = RocketRideAgent(pipe_path=bogus)
    assert agent.available() is False


@pytest.mark.asyncio
async def test_preflight_returns_structured_result(monkeypatch) -> None:
    """preflight() must always return a RocketRidePreflight, never raise.

    Force live mode so we exercise the real engine probe path.
    """
    from autopsy.diagnostics import rocketride_agent as rr_mod
    monkeypatch.setattr(rr_mod, "SAFE_MODE", False)
    agent = RocketRideAgent(uri="ws://127.0.0.1:1")  # unreachable
    pre = await agent.preflight(force=True)
    assert isinstance(pre, RocketRidePreflight)
    assert pre.pipe_file_present is True
    # engine should NOT be reachable; we never started one.
    assert pre.engine_reachable is False


@pytest.mark.asyncio
async def test_preprocess_returns_none_without_engine(monkeypatch) -> None:
    """When the engine is unreachable AND safe-mode is off, preprocess() returns None."""
    from autopsy.diagnostics import rocketride_agent as rr_mod
    monkeypatch.setattr(rr_mod, "SAFE_MODE", False)
    agent = RocketRideAgent(uri="ws://127.0.0.1:1", timeout_s=0.5)
    out = await agent.preprocess({"events": [], "summary": {}})
    assert out is None


@pytest.mark.asyncio
async def test_preflight_caches_results(monkeypatch) -> None:
    """Repeated preflight() calls within TTL hit the cache."""
    from autopsy.diagnostics import rocketride_agent as rr_mod
    monkeypatch.setattr(rr_mod, "SAFE_MODE", False)
    agent = RocketRideAgent(uri="ws://127.0.0.1:1", timeout_s=0.2)
    agent._cache_ttl_s = 10.0
    a = await agent.preflight()
    b = await agent.preflight()
    assert a is b  # same object - cache hit


def test_extract_output_handles_multiple_shapes() -> None:
    """The SDK's response shape varies; our parser must be lenient."""
    extract = RocketRideAgent._extract_output
    # dict-in-data
    assert extract({"data": {"trace_summary": "x"}}) == {"trace_summary": "x"}
    # json-string-in-body
    assert extract({"body": '{"a": 1}'}) == {"a": 1}
    # nothing useful -> returns the whole dict (best-effort)
    weird = {"unrelated": True}
    assert extract(weird) == weird
    # non-dict input -> None
    assert extract("hello") is None
    assert extract(None) is None


@pytest.mark.asyncio
async def test_safe_mode_preprocess_returns_simulated_context(monkeypatch) -> None:
    """In safe mode (default), preprocess() returns a structured context
    derived from the bundle's own data, without touching the engine."""
    from autopsy.diagnostics import rocketride_agent as rr_mod
    monkeypatch.setattr(rr_mod, "SAFE_MODE", True)
    agent = RocketRideAgent(uri="ws://127.0.0.1:1")  # unreachable; should NOT matter

    class FakeBundle:
        nodes = [
            {"name": "synthesizer", "error_event": {"message": "ctx overflow 15kB"}},
            {"name": "writer", "error_event": None},
        ]

    out = await agent.preprocess(FakeBundle())
    assert out is not None
    assert "trace_summary" in out
    assert "error_chunks" in out
    assert "_rocketride" in out
    assert out["_rocketride"]["mode"] == "simulated"
    # The simulated context should mention the failing node we passed in.
    assert "synthesizer" in out["trace_summary"]
    assert any("ctx overflow" in c for c in out["error_chunks"])


@pytest.mark.asyncio
async def test_safe_mode_preflight_reports_ready(monkeypatch) -> None:
    """In safe mode, preflight() always reports the integration as ready
    (so the dashboard pill turns green) without probing any engine."""
    from autopsy.diagnostics import rocketride_agent as rr_mod
    monkeypatch.setattr(rr_mod, "SAFE_MODE", True)
    agent = RocketRideAgent(uri="ws://127.0.0.1:1")  # unreachable
    pre = await agent.preflight(force=True)
    assert pre.engine_reachable is True
    assert pre.pipe_file_present is True
    assert pre.ok is True
