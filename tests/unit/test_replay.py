"""Unit tests for ReplayEngine."""
import pytest

from autopsy import lens
from autopsy.core.replay import ReplayEngine
from autopsy.core.tracer import _default_session_dir, list_sessions, load_bundle


@pytest.mark.asyncio
async def test_simulated_replay_fixes_errors():
    @lens.trace(name="will-fail")
    async def will_fail():
        raise RuntimeError("bad")

    @lens.trace(name="root")
    async def root():
        return await will_fail()

    with pytest.raises(RuntimeError):
        await root()

    bundle = load_bundle(
        _default_session_dir(),
        list_sessions(_default_session_dir())[0]["session_id"])
    # Find the failed node
    err_node_id = None
    for e in bundle.events:
        if e["event_type"] == "node_error":
            err_node_id = e["node_id"]
            break
    assert err_node_id is not None

    engine = ReplayEngine(bundle)
    result = engine.simulated_replay(err_node_id, "fixed")
    assert result["summary"]["status"] == "success"
    assert result["summary"]["error_count"] == 0
    assert result["comparison"]["errors_fixed"] >= 1
    # Replay should be faster than original (we apply 30% speedup).
    assert result["comparison"]["latency_delta_ms"] < 0


def test_simulated_replay_works_without_errors():
    # Empty/no-error bundle still produces a valid replay output.
    from autopsy.core.events import TraceBundle
    b = TraceBundle(session_id="x", events=[
        {"event_type": "node_start", "node_id": "a", "node_name": "n",
         "depth": 0, "node_type": "agent"},
    ], node_index={"a": {"start_event": {"node_name": "n"}}})
    engine = ReplayEngine(b)
    r = engine.simulated_replay("a", "test")
    assert r["target_node_id"] == "a"
    assert r["summary"]["status"] == "success"
