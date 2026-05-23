"""Simple agent example - succeeds.

Run with: autopsy run examples/simple_agent.py

Demonstrates a multi-step async pipeline that completes successfully. Use this
to show what a happy-path trace looks like in the dashboard.
"""
import asyncio
from autopsy import lens


@lens.trace(name="fetch-data", node_type="tool")
async def fetch_data(source: str) -> dict:
    await asyncio.sleep(0.05)
    return {"source": source, "items": ["a", "b", "c"]}


@lens.trace(name="analyze")
async def analyze(data: dict) -> dict:
    await asyncio.sleep(0.04)
    return {"count": len(data.get("items", [])), "source": data["source"]}


@lens.trace(name="format-output")
async def format_output(analysis: dict) -> str:
    await asyncio.sleep(0.02)
    return f"Found {analysis['count']} items from {analysis['source']}"


@lens.trace(name="simple-pipeline")
async def simple_pipeline(query: str) -> str:
    data = await fetch_data(query)
    analysis = await analyze(data)
    out = await format_output(analysis)
    return out


if __name__ == "__main__":
    result = asyncio.run(simple_pipeline("demo-source"))
    print("result:", result)
