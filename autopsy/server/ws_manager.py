"""WebSocket connection manager: broadcasts events to all connected dashboards."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, is_dataclass
from typing import Any

logger = logging.getLogger("autopsy.ws")


def _json_default(obj: Any) -> Any:
    try:
        return str(obj)
    except Exception:
        return "<unserializable>"


class WSManager:
    def __init__(self):
        self.active_connections: list = []
        self._lock = asyncio.Lock()

    async def connect(self, ws) -> None:
        await ws.accept()
        async with self._lock:
            self.active_connections.append(ws)

    async def disconnect(self, ws) -> None:
        async with self._lock:
            try:
                self.active_connections.remove(ws)
            except ValueError:
                pass

    async def broadcast(self, message: dict) -> None:
        """Send a JSON-encoded message to every connected client."""
        try:
            payload = json.dumps(message, default=_json_default)
        except Exception:
            logger.exception("autopsy.ws: serialize failed")
            return
        # Snapshot connections so we don't hold the lock during sends.
        async with self._lock:
            conns = list(self.active_connections)
        dead = []
        for ws in conns:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    try:
                        self.active_connections.remove(ws)
                    except ValueError:
                        pass

    async def broadcast_event(self, event) -> None:
        try:
            data = asdict(event) if is_dataclass(event) else event
        except Exception:
            data = {"event_type": getattr(event, "event_type", "unknown")}
        await self.broadcast({"type": "event", "data": data})


# Singleton.
ws_manager = WSManager()
