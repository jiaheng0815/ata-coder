# -*- coding: utf-8 -*-
"""Async event queue for agent→UI communication — part of the core infrastructure.

Both agent and UI run as asyncio tasks on the same event loop.
asyncio.Queue provides coroutine-safe FIFO semantics without locks.
"""
import asyncio
from typing import Any, Optional

logger = __import__("logging").getLogger(__name__)

__all__ = ["EventQueue"]


class EventQueue:
    """Async event queue wrapping asyncio.Queue.

    Usage:
        eq = EventQueue()
        # Agent task:
        await eq.put(TextDeltaEvent("hello"))
        # UI task:
        async for event in eq.drain():
            ui.on_event(event)
    """

    def __init__(self, maxsize: int = 0):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._total_put = 0
        self._total_get = 0

    async def put(self, event: Any) -> None:
        """Enqueue an event (coroutine-safe, called from agent task)."""
        await self._queue.put(event)
        self._total_put += 1

    def put_nowait(self, event: Any) -> None:
        """Enqueue an event without blocking."""
        self._queue.put_nowait(event)
        self._total_put += 1

    async def get(self, timeout: Optional[float] = None) -> Optional[Any]:
        """Get one event, blocking with optional timeout."""
        try:
            if timeout is not None:
                event = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            else:
                event = await self._queue.get()
            self._total_get += 1
            return event
        except asyncio.TimeoutError:
            return None

    async def drain(self) -> list[Any]:
        """Get ALL pending events without blocking."""
        events: list[Any] = []
        while not self._queue.empty():
            try:
                events.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        count = len(events)
        self._total_get += count
        return events

    def count(self) -> int:
        """Return number of pending events (exact — asyncio.Queue.qsize is precise)."""
        return self._queue.qsize()

    @property
    def total_put(self) -> int:
        return self._total_put

    @property
    def total_get(self) -> int:
        return self._total_get

    async def clear(self) -> None:
        """Discard all pending events."""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._total_get = self._total_put
