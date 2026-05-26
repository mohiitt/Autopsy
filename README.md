# Autopsy

> _Your agent died. Here's why._
> One decorator. Full agent visibility. AI that diagnoses your AI.

`autopsy` wraps any async LLM agent with a single decorator, captures a full execution trace, streams it live to a local web dashboard, and runs an AI-powered diagnostic chain. It identifies the root cause, suggests a code fix, and lets you replay the failing slice with the fix applied—all from the dashboard.

Three AI products work together on every diagnosis:

- **RocketRide pipeline**: Grounds the trace (PII scrub → trace summarize → similar-case retrieval).
- **GMI Cloud** (DeepSeek-V3.2 / Qwen3-Next-80B): Runs root-cause reasoning.
- **Google Gemini 2.5 Pro**: Serves as the long-context fallback if GMI is rate-limited.

## Install

```bash
pip install -e .
# Optional: enable the live RocketRide integration
pip install -e '.[rocketride]'
```

## Quickstart

1. Instrument your agent:

```python
from autopsy import lens

@lens.trace
async def my_agent(query: str):
    # your existing agent code - zero changes
    ...
```

2. Run the agent and launch the dashboard:

```bash
autopsy run agent.py
# Dashboard opens at http://localhost:7823
```

## CLI

```bash
autopsy run examples/financial_research_pipeline.py   # Run an agent + dashboard
autopsy serve                                          # Start dashboard only
autopsy sessions                                       # List saved traces
autopsy diagnose <session_id>                          # Run AI root-cause analysis
autopsy replay <session_id>                            # Simulated replay with fix
autopsy clean --all                                    # Wipe local sessions
```

## Key Features

- **Live DAG Trace**: Visual flow of agent hops, tool calls, and LLM completions with real-time streaming.
- **Multi-AI Diagnostics**: Automated root-cause reasoning with GMI Cloud, Google Gemini, and RocketRide pre-processing.
- **Time-Travel Replay**: Test code changes on the failing slice instantly, then hot-patch the running loop.
- **Latency Insights**: View exact execution times for each agent step.
- **OpenAI SDK Auto-Instrumentation**: Transparently intercept OpenAI-compatible API calls.
- **Zero-Config UI**: Single-page dashboard running locally with no Node.js required.

## Environment Setup

Copy `.env.example` to `.env` and fill in your keys:

```bash
# Required for AI-powered diagnostics
GMI_API_KEY=...
GMI_DEFAULT_MODEL=deepseek-ai/DeepSeek-V3.2
GMI_FALLBACK_MODEL=Qwen/Qwen3-Next-80B-A3B-Instruct

# Optional: long-context fallback
GOOGLE_AI_API_KEY=...
GEMINI_MODEL=gemini-2.5-pro

# Optional: RocketRide live engine (WS URL and key)
ROCKETRIDE_URI=ws://localhost:5565
ROCKETRIDE_APIKEY=
ROCKETRIDE_OPENAI_KEY=${GMI_API_KEY}
ROCKETRIDE_OPENAI_BASE_URL=https://api.gmi-serving.com/v1
ROCKETRIDE_OPENAI_MODEL=Qwen/Qwen3-Next-80B-A3B-Instruct
AUTOPSY_ROCKETRIDE_SAFE_MODE=1               # 1 = simulated (default); 0 = live engine

AUTOPSY_PORT=7823
```

_Note: If no API keys are provided, autopsy falls back to a built-in heuristic diagnoser._

## Demos

### 1. Continuous Live Loop (Financial Research)

```bash
autopsy run examples/financial_research_pipeline.py
```

This runs a multi-agent loop with a deliberately broken synthesizer that overflows context.

1. **Watch**: The graph runs live. The synthesizer node will fail (turn red).
2. **Diagnose**: Click the red node -> **🔍 Diagnose this node** to run the diagnosis.
3. **Replay & Fix**: Click **"Apply fix & replay"**. The trace goes green, and the live pipeline hot-patches itself.

Configure loop knobs using environment variables:

- `AUTOPSY_LOOP_DELAY_S` (default: 8)
- `AUTOPSY_LATENCY_SCALE` (default: 2.5)

### 2. Single-Shot Broken Agent

```bash
autopsy run examples/broken_agent.py
```

A lightweight, single-shot 4-step pipeline that fails with a `JSONDecodeError`. Ideal for a quick walk-through.

## RocketRide Integration

`autopsy` optionally integrates with **RocketRide** for trace pre-processing:

- **Visual Editor**: Open and edit [`pipelines/autopsy_diagnose.pipe`](pipelines/autopsy_diagnose.pipe) in the RocketRide Studio extension.
- **Safe Mode**: Runs in simulated safe mode by default (`AUTOPSY_ROCKETRIDE_SAFE_MODE=1`), which requires no external services. Set to `0` to hit the live RocketRide engine.
- **Smoke Test**: Validate live integration using:
  ```bash
  python scripts/test_rocketride_pipe.py
  ```

## Architecture

- **`autopsy/core/`**: Core tracing logic, event definitions, OpenAI SDK monkey-patching, and replay runner.
- **`autopsy/diagnostics/`**: GMI, Gemini, and RocketRide diagnoser clients.
- **`autopsy/server/`**: FastAPI server and the Vanilla-JS dashboard UI.
- **`autopsy/cli/`**: Click-based CLI commands.
- **`examples/`**: Demo pipelines.
- **`pipelines/`**: RocketRide pipeline definitions.

## Tests & Development

Run the test suite and code quality checks:

```bash
pytest tests/
ruff check autopsy tests
```

## Robustness & Design

- **Fault Tolerant**: Code instrumentation never crashes the parent agent.
- **Heuristic Fallback**: Always provides a fallback diagnosis if external APIs are unreachable.
- **Atomic File Access**: Local session updates use atomic writes and file-locking.
- **Graceful degradation**: RocketRide is completely optional; fallback to standard GMI is automatic.

## Demo video link:

### https://youtu.be/FpxuyQTs2Hg

## License

MIT
