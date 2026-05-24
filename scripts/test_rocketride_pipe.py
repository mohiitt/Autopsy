"""
Standalone smoke test for the RocketRide diagnostic pipeline.

Runs `pipelines/autopsy_diagnose.pipe` against the local engine
(ws://localhost:5565) ONCE with a small fake trace and prints the result.

If this script succeeds, the dashboard's Diagnose button will work too. If it
fails, autopsy will gracefully fall back to direct GMI - the demo still runs.

Usage:
    .venv/bin/python scripts/test_rocketride_pipe.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# Ensure repo root is on sys.path so `from autopsy...` imports work when
# this script is run directly.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Load .env BEFORE importing rocketride so its config picks up our vars.
try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
except Exception:
    pass


PIPE_PATH = REPO_ROOT / "pipelines" / "autopsy_diagnose.pipe"

FAKE_TRACE_QUESTION = (
    "Diagnose this autopsy trace. The financial-research-orchestrator "
    "ran a pipeline with 3 parallel researcher agents. The synthesizer "
    "node raised ValueError('Context window exceeded: 15636 chars vs "
    "12000 char limit') after the 3 researchers each returned ~5KB "
    "briefs that were concatenated without truncation. Return the root "
    "cause, the failing node, the error category, a confidence score, "
    "and a runnable fix snippet."
)


async def main() -> int:
    print("=" * 60)
    print("RocketRide pipeline smoke test")
    print("=" * 60)
    print(f"pipe   : {PIPE_PATH}")
    print(f"exists : {PIPE_PATH.exists()}")

    # If the .env-provided URI is unreachable, fall back to engine discovery.
    explicit_uri = os.environ.get("ROCKETRIDE_URI", "").strip()
    uri = explicit_uri
    if not uri or uri == "ws://localhost:5565":
        from autopsy.diagnostics.rocketride_discover import discover_local_engine_uri
        discovered = discover_local_engine_uri()
        if discovered:
            print(f"uri    : {discovered} (auto-discovered)")
            uri = discovered
        else:
            print(f"uri    : {uri or '(unset)'} (no engine discovered either)")
    else:
        print(f"uri    : {uri} (from .env)")
    print(f"apikey : "
          f"{'set' if os.environ.get('ROCKETRIDE_APIKEY') else 'unset (local mode)'}")
    print(f"llm key: "
          f"{'set' if os.environ.get('ROCKETRIDE_OPENAI_KEY') else 'unset!'}")
    print(f"llm url: {os.environ.get('ROCKETRIDE_OPENAI_BASE_URL', '(unset)')}")
    print(f"llm mdl: {os.environ.get('ROCKETRIDE_OPENAI_MODEL', '(unset)')}")
    print()

    try:
        from rocketride import RocketRideClient
        from rocketride.schema import Question
    except ImportError as e:
        print(f"[ERR] rocketride SDK not installed: {e}")
        print("      pip install rocketride")
        return 2

    client = RocketRideClient(uri=uri, auth="autopsy-dev")

    try:
        print("[1/4] connecting...", flush=True)
        await asyncio.wait_for(client.connect(), timeout=10.0)
        print(f"      connected: {client.is_connected()}")
        print(f"      info     : {client.get_connection_info()}")
    except Exception as e:
        print(f"[ERR] connect failed: {type(e).__name__}: {e}")
        print("      Is the engine running? "
              "(VS Code RocketRide extension or `docker run ...`)")
        return 3

    # Push ROCKETRIDE_* env vars into the engine so the pipe's ${VAR}
    # substitutions resolve correctly. The engine only reads ROCKETRIDE_*
    # for security; that's why we use ROCKETRIDE_OPENAI_KEY etc.
    try:
        rr_env = {k: v for k, v in os.environ.items()
                  if k.startswith("ROCKETRIDE_") and v}
        if rr_env:
            print(f"[1.5] pushing env to engine: {sorted(rr_env.keys())}", flush=True)
            await asyncio.wait_for(
                client.account.set_env(scope="user", env=rr_env),
                timeout=10.0)
    except Exception as e:
        print(f"      (env push failed, continuing: {type(e).__name__}: {e})")

    try:
        print("[2/4] loading pipeline...", flush=True)
        # Terminate any existing instance first so config env-var changes are
        # picked up. Ignore failures (no existing task).
        try:
            existing = await asyncio.wait_for(
                client.get_task_token(
                    project_id="b91deb36-4110-47eb-bd85-78efa3bdb45e",
                    source="chat_1"),
                timeout=5.0)
            if existing:
                print(f"      terminating prior pipeline {existing}...")
                await asyncio.wait_for(client.terminate(existing), timeout=10.0)
        except Exception as e:
            print(f"      (no prior pipeline to clean: {type(e).__name__})")

        result = await asyncio.wait_for(
            client.use(filepath=str(PIPE_PATH)),
            timeout=45.0)
        token = result.get("token")
        if not token:
            print(f"[ERR] use() returned no token: {result}")
            return 4
        print(f"      token: {token}")

        print("[3/4] sending chat question...", flush=True)
        q = Question()
        q.addQuestion(FAKE_TRACE_QUESTION)
        response = await asyncio.wait_for(
            client.chat(token=token, question=q), timeout=60.0)

        print("[4/4] response:")
        print("-" * 60)
        print(json.dumps(response, indent=2, default=str)[:4000])
        print("-" * 60)

        # Try to extract the answer text the same way our agent does.
        answers = response.get("answers") if isinstance(response, dict) else None
        if answers and isinstance(answers, list) and answers:
            first = answers[0]
            text = (first.get("answer") or first.get("text")
                    or first.get("content")) if isinstance(first, dict) else str(first)
            print()
            print("=== first answer (truncated) ===")
            print(str(text)[:1200])
            print("===")

        # Clean up.
        try:
            await client.terminate(token)
        except Exception as e:
            print(f"[warn] terminate failed: {e}")

        return 0
    except asyncio.TimeoutError:
        print("[ERR] pipeline run timed out")
        return 5
    except Exception as e:
        print(f"[ERR] pipeline run failed: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return 6
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
