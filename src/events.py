"""In-process pub/sub event bus.

Browser tabs subscribe via Server-Sent Events to /events; the worker thread
publishes job state changes here. One bus per process, multiple subscribers.

Why not WebSocket: SSE is one-way (server→browser), goes over plain HTTP,
auto-reconnects, native EventSource API. Right tool for "push job updates".

Why not Redis/RabbitMQ: single process, single user. Adding a broker adds
operational weight with zero observable benefit.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from typing import Any

log = logging.getLogger(__name__)


class EventBus:
    def __init__(self, history: int = 100):
        self._lock = threading.Lock()
        # subscriber id → asyncio.Queue
        self._subs: dict[int, "asyncio.Queue"] = {}
        self._next_id = 0
        # Replay buffer for late subscribers.
        self._history: deque = deque(maxlen=history)

    def publish(self, kind: str, payload: dict | None = None) -> None:
        """Threadsafe publish. Worker thread (sync) → asyncio queues."""
        evt = {"ts": time.time(), "kind": kind, "data": payload or {}}
        with self._lock:
            self._history.append(evt)
            queues = list(self._subs.values())
        for q in queues:
            # Non-blocking put: if a slow subscriber's queue fills up, drop
            # the event for them. They'll see the next one.
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                log.debug("event queue full; dropping for slow subscriber")

    def subscribe(self) -> tuple[int, "asyncio.Queue"]:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        with self._lock:
            sid = self._next_id
            self._next_id += 1
            self._subs[sid] = q
        return sid, q

    def unsubscribe(self, sid: int) -> None:
        with self._lock:
            self._subs.pop(sid, None)

    def replay(self) -> list[dict]:
        with self._lock:
            return list(self._history)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)


def format_sse(evt: dict) -> str:
    """Format a dict as one SSE message frame."""
    return f"event: {evt['kind']}\ndata: {json.dumps(evt)}\n\n"


# Module-level singleton — one bus per process.
_bus: EventBus | None = None


def get_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus
