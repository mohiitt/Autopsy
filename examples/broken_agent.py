"""Broken agent demo for autopsy.

Run with: autopsy run examples/broken_agent.py

What it does:
1. A "research" agent takes a hardcoded query.
2. Planner sub-agent decides to search + summarize.
3. Search tool returns a large fake article (~4000 tokens).
4. Summarizer sub-agent gets overloaded context -> raises a JSONDecodeError
   (deterministic; no LLM call needed - guaranteed to fail in <1ms).
5. Response assembler is blocked.

This triggers the demo flow:
- Dashboard shows red nodes
- "Diagnose" -> GMI Cloud identifies bad_json + context_overflow
- "Replay" -> simulated success, dashboard goes green
"""
import asyncio
import json

from autopsy import lens


LARGE_FAKE_ARTICLE = (
    "BREAKING: AI safety researchers warn of new alignment challenges. "
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 400
)


@lens.trace(name="planner", node_type="agent")
async def planner_agent(query: str) -> dict:
    await asyncio.sleep(0.05)
    return {
        "steps": ["search", "summarize", "assemble"],
        "intent": f"research {query!r}",
    }


@lens.trace(name="search-tool", node_type="tool")
async def search_tool(query: str) -> str:
    await asyncio.sleep(0.08)
    return LARGE_FAKE_ARTICLE


@lens.trace(name="summarizer", node_type="agent")
async def summarizer_agent(context: str) -> dict:
    """Guaranteed deterministic failure when context is oversized."""
    await asyncio.sleep(0.03)
    CONTEXT_LIMIT_CHARS = 8000
    if len(context) > CONTEXT_LIMIT_CHARS:
        truncated = '{"summary": "AI safety is'
        raise json.JSONDecodeError(
            "Context overflow: model returned malformed JSON "
            "(input was {} chars, safe limit is {})".format(
                len(context), CONTEXT_LIMIT_CHARS),
            doc=truncated, pos=23,
        )
    return {"summary": context[:200], "key_points": []}


@lens.trace(name="response-assembler", node_type="agent")
async def response_assembler(summary_data: dict) -> str:
    await asyncio.sleep(0.01)
    return f"Summary: {summary_data['summary']}"


@lens.trace(name="news-research-agent")
async def research_agent(query: str) -> str:
    _ = await planner_agent(query)
    search_results = await search_tool(query)
    summary = await summarizer_agent(search_results)
    return await response_assembler(summary)


if __name__ == "__main__":
    try:
        result = asyncio.run(research_agent(
            "Summarize the latest news about AI safety"))
        print("result:", result)
    except Exception as e:
        # Expected - the demo intentionally fails. Tracing captured it.
        print(f"agent failed as expected: {type(e).__name__}: {e}")
