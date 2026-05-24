"""
Locate the RocketRide engine when the VS Code/Cursor extension launches it.

The extension runs the engine binary with `--port=0`, which means the engine
picks a random ephemeral port and reports it back to the extension over its
own (extension-managed) channel. External clients - including autopsy's Python
SDK client - have no way to know that port without inspecting the process.

This helper does the inspection. It looks for a running process whose command
line matches the engine signature (`engine ... --autoterm ./ai/eaas.py`) and
then uses `lsof -p <pid>` to extract the TCP port that process is LISTENing on.

Returns a `ws://localhost:<port>` URI or None if no engine is found.

This is best-effort and only used as a fallback when ROCKETRIDE_URI is not
explicitly set. It costs ~50ms and is cached by `RocketRideAgent`.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

ENGINE_CMD_SIGNATURE = "--autoterm"  # robust signature for the extension's engine


def _find_engine_pid() -> Optional[int]:
    """Find the PID of the local RocketRide engine if it's running."""
    if shutil.which("pgrep"):
        try:
            out = subprocess.run(
                ["pgrep", "-f", ENGINE_CMD_SIGNATURE],
                capture_output=True, text=True, timeout=2.0)
            pids = [int(p) for p in out.stdout.split() if p.strip().isdigit()]
            if pids:
                # Pick the smallest pid (usually the parent if there are children).
                return min(pids)
        except Exception as e:
            logger.debug("pgrep failed: %s", e)

    # Fallback: parse `ps` directly.
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid,command"],
            capture_output=True, text=True, timeout=2.0)
        for line in out.stdout.splitlines():
            if ENGINE_CMD_SIGNATURE in line and "/engine" in line:
                parts = line.strip().split(None, 1)
                if parts and parts[0].isdigit():
                    return int(parts[0])
    except Exception as e:
        logger.debug("ps fallback failed: %s", e)
    return None


_LISTEN_RE = re.compile(
    r"^\S+\s+(\d+)\s+\S+\s+\S+\s+(?:IPv[46])\s+\S+\s+\S+\s+TCP\s+"
    r"(?:127\.0\.0\.1|\[::1\]):(\d+)\s+\(LISTEN\)"
)


def _listening_port_for_pid(pid: int) -> Optional[int]:
    """Return the first localhost TCP port that <pid> is LISTENing on.

    macOS lsof OR's `-p` with `-iTCP`, so we can't just filter by pid that way.
    Instead we list ALL listening sockets and grep for the pid column.
    """
    if not shutil.which("lsof"):
        return None
    try:
        out = subprocess.run(
            ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=3.0)
    except Exception as e:
        logger.debug("lsof failed: %s", e)
        return None
    pid_str = str(pid)
    for line in out.stdout.splitlines():
        m = _LISTEN_RE.match(line)
        if not m:
            continue
        line_pid, port_str = m.group(1), m.group(2)
        if line_pid == pid_str:
            try:
                return int(port_str)
            except ValueError:
                continue
    return None


def discover_local_engine_uri() -> Optional[str]:
    """Best-effort: find the RocketRide engine's ws:// URI on this machine.

    Returns None if the engine is not running or the port can't be determined.
    Never raises.
    """
    try:
        pid = _find_engine_pid()
        if pid is None:
            return None
        port = _listening_port_for_pid(pid)
        if port is None:
            return None
        uri = f"ws://localhost:{port}"
        logger.debug("RocketRide engine discovered at %s (pid=%d)", uri, pid)
        return uri
    except Exception as e:
        logger.debug("engine discovery failed: %s", e)
        return None
