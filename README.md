# autopsy

> _Your agent died. Here's why._
> One decorator. Full agent visibility. AI that diagnoses your AI.

`autopsy` wraps any async LLM agent with a single decorator, captures a full execution trace, streams it to a local web dashboard (auto-opened in your browser), and runs a GMI Cloud–powered diagnostics agent that identifies root causes and suggests fixes in seconds.

## Install

```bash
pip install -e .
```

## Quickstart

```python
from autopsy import lens

@lens.trace
async def my_agent(query: str):
    # your existing agent code — zero changes
    ...
```

```bash
autopsy run agent.py
# Dashboard opens at http://localhost:7823
```

## CLI

```bash
autopsy run examples/broken_agent.py    # run an agent + dashboard
autopsy serve                            # start dashboard only
autopsy sessions                         # list saved traces
autopsy diagnose <session_id>            # AI root-cause analysis
autopsy replay <session_id>              # simulated replay with fix
autopsy clean --all                      # wipe local sessions
autopsy deploy                           # export sessions to share
```

## What you get

- **Live DAG** of every agent hop, tool call, and LLM completion
- **Root cause diagnosis** via GMI Cloud (DeepSeek/Qwen) in seconds
- **Heuristic fallback** that still produces a useful diagnosis offline
- **Time-travel replay** — simulated replay shows the fix going green
- **Latency breakdown** — see exactly where your agent is slow
- **Works with everything** — OpenAI SDK, anything that uses OpenAI-compatible APIs (GMI, Together, etc.) is intercepted transparently
- **Zero-config dashboard** — a polished vanilla-JS dashboard ships with the package; no Node.js required

## Environment

Copy `.env.example` to `.env` and fill in your keys:

```bash
GMI_API_KEY=...                              # https://gmi.ai/console
GMI_DEFAULT_MODEL=deepseek-ai/DeepSeek-V3.2  # fast, strong reasoning
GMI_FALLBACK_MODEL=Qwen/Qwen3-Next-80B-A3B-Instruct
GOOGLE_AI_API_KEY=...                        # for long-context (>32k tokens)
AUTOPSY_PORT=7823
```

If `GMI_API_KEY` is not set, autopsy falls back to a built-in heuristic diagnoser that still produces a sensible root-cause analysis — so the demo always works.

## Demo: the broken agent

```bash
autopsy run examples/broken_agent.py
```

The `broken_agent.py` is a deliberately broken multi-step pipeline:

1. **planner** decides how to research a query
2. **search-tool** returns a too-large article
3. **summarizer** receives an oversized context and raises `JSONDecodeError`
4. **response-assembler** is blocked

The dashboard lights up two red nodes. Click **🔍 Diagnose this node**: GMI Cloud identifies the failure as `bad_json` caused by `context_overflow`, suggests a chunking fix, and shows working Python code. Click **↻ Replay from here**: the dashboard shows a simulated replay where both nodes go green, with latency and token deltas.

## Architecture

```
autopsy/
  core/
    events.py        # event dataclasses
    tracer.py        # TraceSession - lifetime of one run
    decorator.py     # @lens.trace
    interceptor.py   # monkey-patches OpenAI SDK
    replay.py        # ReplayEngine (simulated + live)
  diagnostics/
    gmi_agent.py     # GMI Cloud diagnostics
    gemini_agent.py  # long-context fallback
    prompts.py       # prompt templates + heuristics
  server/
    app.py           # FastAPI + WebSocket
    ws_manager.py    # broadcast manager
    _dashboard.html  # built-in single-page dashboard
  cli/main.py        # click-based CLI
  __init__.py        # public: lens, LensConfig, TraceBundle, DiagnosisResult
examples/
  broken_agent.py    # hackathon demo
  simple_agent.py    # happy-path demo
tests/
  unit/, integration/  # pytest suite (13 tests)
```

## Robustness notes

- The tracer never raises — a bug in instrumentation never crashes the user's agent
- Diagnostics has a strong heuristic fallback so the demo works even if GMI is down
- Session storage tries `~/.autopsy/sessions`, then `./.autopsy/sessions`, then `/tmp` — picks the first writable location
- WebSocket auto-reconnects with exponential backoff
- The OpenAI interceptor is install-once / refcounted so multiple concurrent sessions don't fight
- All session writes are atomic (write to .tmp then rename); index updates are file-locked

## License

MIT
