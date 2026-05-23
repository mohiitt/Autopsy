"""Unit tests for @lens.trace decorator."""
import asyncio
import json
import os
from pathlib import Path

import pytest

from autopsy import lens
from autopsy.core.tracer import (
    _default_session_dir, get_current_session, list_sessions, load_bundle,
)


@pytest.mark.asyncio
async def test_decorator_basic_success():
    @lens.trace(name="x")
    async def f(q):
        return q + "!"

    out = await f("hi")
    assert out == "hi!"
    assert get_current_session() is None

    sessions = list_sessions(_default_session_dir())
    assert len(sessions) == 1
    s = sessions[0]
    assert s["agent_name"] == "x"
    assert s["status"] == "success"
    assert s["error_count"] == 0


@pytest.mark.asyncio
async def test_decorator_nested_dag():
    @lens.trace(name="child")
    async def child(x):
        return x * 2

    @lens.trace(name="parent")
    async def parent(q):
        a = await child(3)
        b = await child(5)
        return a + b

    out = await parent("hi")
    assert out == 16
    assert get_current_session() is None

    sessions = list_sessions(_default_session_dir())
    sid = sessions[0]["session_id"]
    bundle = load_bundle(_default_session_dir(), sid)
    assert bundle.summary["node_count"] == 3
    # Both children should be linked to parent.
    parents = {e[0] for e in bundle.dag_edges}
    children = {e[1] for e in bundle.dag_edges}
    assert len(parents) == 1
    assert len(children) == 2


@pytest.mark.asyncio
async def test_decorator_propagates_exceptions_and_emits_error_event():
    @lens.trace(name="boom")
    async def boom():
        raise ValueError("nope")

    with pytest.raises(ValueError):
        await boom()

    assert get_current_session() is None
    bundle = load_bundle(
        _default_session_dir(),
        list_sessions(_default_session_dir())[0]["session_id"])
    types = [e["event_type"] for e in bundle.events]
    assert "node_error" in types
    assert bundle.summary["error_count"] >= 1
    assert bundle.summary["status"] == "error"


@pytest.mark.asyncio
async def test_decorator_no_session_leak_between_runs():
    @lens.trace(name="solo")
    async def solo():
        return 1

    await solo()
    assert get_current_session() is None
    await solo()
    assert get_current_session() is None
    assert len(list_sessions(_default_session_dir())) == 2
