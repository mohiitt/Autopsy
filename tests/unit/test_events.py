"""Unit tests for event dataclasses."""
import json
from dataclasses import asdict

from autopsy.core.events import (
    LLMRequestEvent,
    LLMResponseEvent,
    NodeEndEvent,
    NodeErrorEvent,
    NodeStartEvent,
    SessionEndEvent,
    SessionStartEvent,
    TraceBundle,
    DiagnosisResult,
)


def test_all_events_serialize():
    events = [
        SessionStartEvent(session_id="s", agent_name="a", input_query="q"),
        NodeStartEvent(node_id="n1", node_name="x"),
        NodeEndEvent(node_id="n1", duration_ms=1.0, output_data={"k": "v"}),
        NodeErrorEvent(node_id="n1", error_type="ValueError", error_message="oops"),
        LLMRequestEvent(node_id="n1", model="m", messages=[{"role": "u"}]),
        LLMResponseEvent(node_id="n1", model="m", content="hi", total_tokens=10),
        SessionEndEvent(session_id="s", status="success"),
    ]
    for ev in events:
        d = asdict(ev)
        assert "event_type" in d
        s = json.dumps(d)
        assert json.loads(s) == d


def test_trace_bundle_roundtrips():
    b = TraceBundle(
        session_id="s", created_at=1.0, agent_name="a",
        input_query="q", events=[{"event_type": "session_start"}],
        dag_edges=[["a", "b"]], summary={"status": "success"},
    )
    s = json.dumps(asdict(b))
    out = json.loads(s)
    assert out["session_id"] == "s"
    assert out["dag_edges"] == [["a", "b"]]


def test_diagnosis_result_defaults():
    d = DiagnosisResult(root_cause="x")
    assert d.root_cause == "x"
    assert d.confidence == 0.0
    assert d.error_category == "other"
