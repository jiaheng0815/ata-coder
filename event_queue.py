# -*- coding: utf-8 -*-
"""
Thread-safe event queue for agent→UI communication.

The agent thread enqueues events; the main (UI) thread dequeues
and processes them. This keeps Rich/prompt_toolkit rendering
on the main thread while the agent blocks on I/O.

Usage:
    >>> from ata_coder.event_queue import EventQueue
    >>> eq = EventQueue()
    >>> # Agent thread:
    >>> eq.put(TextDeltaEvent("hello"))
    >>> # UI thread:
    >>> for event in eq.drain():
    ...     ui.on_event(event)
    >>> print(f"Pending: {eq.count()}, Total put: {eq.total_put}")

Thread Safety:
    All methods use threading.Lock internally. put() and drain()
    can be called from different threads safely.
"""

import queue
import threading
from typing import Any, Optional

logger = __import__("logging").getLogger(__name__)

__all__ = ["EventQueue"]


class EventQueue:
    """
    Thread-safe event queue wrapping queue.Queue.

    Usage:
        eq = EventQueue()
        # Agent thread:
        eq.put(TextDeltaEvent("hello"))
        # UI thread:
        for event in eq.drain():
            ui.on_event(event)
    """

    def __init__(self, maxsize: int = 0):
        self._queue: queue.Queue = queue.Queue(maxsize=maxsize)
        self._lock = threading.Lock()
        self._total_put = 0
        self._total_get = 0

    def put(self, event: Any) -> None:
        """Enqueue an event (thread-safe, called from agent thread)."""
        self._queue.put(event)
        with self._lock:
            self._total_put += 1

    def get(self, timeout: Optional[float] = None) -> Optional[Any]:
        """
        Get one event, blocking with optional timeout.
        Returns None if queue is empty and timeout expires.
        """
        try:
            event = self._queue.get(timeout=timeout)
            with self._lock:
                self._total_get += 1
            return event
        except queue.Empty:
            return None

    def drain(self) -> list[Any]:
        """Get ALL pending events without blocking."""
        events: list[Any] = []
        while not self._queue.empty():
            try:
                event = self._queue.get_nowait()
                events.append(event)
                with self._lock:
                    self._total_get += 1
            except queue.Empty:
                break
        return events

    def count(self) -> int:
        """Return approximate number of pending events."""
        return self._queue.qsize()

    @property
    def total_put(self) -> int:
        with self._lock:
            return self._total_put

    @property
    def total_get(self) -> int:
        with self._lock:
            return self._total_get

    def clear(self) -> None:
        """Discard all pending events (thread-safe)."""
        with self._lock:
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
            self._total_get = self._total_put
