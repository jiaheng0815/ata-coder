# -*- coding: utf-8 -*-
"""
Thread supervisor — heartbeat monitoring, timeout detection, and fencing.

Ensures that one thread hanging does not bring down the entire system.
Each watched thread calls `heartbeat()` periodically; the watchdog
detects timeouts and can cancel/fence hung threads.
"""

import logging
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

__all__ = ["ThreadSupervisor"]

class ThreadSupervisor:
    """
    Monitors registered threads for hangs and crashes.

    Usage:
        supervisor = ThreadSupervisor(default_timeout=300.0)
        cancel_event = threading.Event()
        supervisor.register("agent-main", cancel_event)
        supervisor.start_watchdog()

        # In the agent thread:
        while not cancel_event.is_set():
            supervisor.heartbeat("agent-main")
            ...
    """

    def __init__(self, default_timeout: float = 300.0):
        self._default_timeout = default_timeout
        self._heartbeats: dict[str, float] = {}       # thread_name → timestamp
        self._cancel_events: dict[str, threading.Event] = {}
        self._timeouts: dict[str, float] = {}          # per-thread timeout overrides
        self._on_timeout_callbacks: dict[str, Callable] = {}
        self._lock = threading.Lock()
        self._watchdog: Optional[threading.Thread] = None
        self._running = False
        self._timeout_count: dict[str, int] = {}
        self._last_logged: dict[str, float] = {}  # rate-limit error logging
        self._log_interval: float = 30.0  # log at most every N seconds per thread

    def register(
        self,
        name: str,
        cancel_event: Optional[threading.Event] = None,
        timeout: Optional[float] = None,
        on_timeout: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Register a thread for supervision."""
        with self._lock:
            self._heartbeats[name] = time.time()
            if cancel_event:
                self._cancel_events[name] = cancel_event
            if timeout is not None:
                self._timeouts[name] = timeout
            if on_timeout:
                self._on_timeout_callbacks[name] = on_timeout
            self._timeout_count.setdefault(name, 0)
        logger.debug("Supervisor registered: %s (timeout=%ss)", name,
                     timeout or self._default_timeout)

    def unregister(self, name: str) -> None:
        """Stop supervising a thread."""
        with self._lock:
            self._heartbeats.pop(name, None)
            self._cancel_events.pop(name, None)
            self._timeouts.pop(name, None)
            self._on_timeout_callbacks.pop(name, None)
            self._timeout_count.pop(name, None)

    def heartbeat(self, name: str) -> None:
        """Called periodically by the watched thread to signal aliveness."""
        with self._lock:
            self._heartbeats[name] = time.time()

    def start_watchdog(self, interval: float = 1.0) -> None:
        """Start background watchdog that checks for timeouts."""
        if self._watchdog and self._watchdog.is_alive():
            return
        self._running = True
        self._watchdog = threading.Thread(
            target=self._watchdog_loop,
            args=(interval,),
            daemon=True,
            name="thread-supervisor",
        )
        self._watchdog.start()
        logger.info("Watchdog started (interval=%ss)", interval)

    def stop_watchdog(self) -> None:
        """Stop the watchdog thread."""
        self._running = False
        if self._watchdog:
            self._watchdog.join(timeout=5.0)
            self._watchdog = None

    def _watchdog_loop(self, interval: float) -> None:
        """Internal watchdog loop."""
        while self._running:
            # Snapshot timed-out threads under lock, then act outside lock
            # to avoid deadlock if callbacks call register/unregister/heartbeat.
            timed_out: list[tuple[str, float, int, Callable | None, threading.Event | None]] = []
            with self._lock:
                now = time.time()
                for name, last_hb in list(self._heartbeats.items()):
                    timeout = self._timeouts.get(name, self._default_timeout)
                    elapsed = now - last_hb
                    if elapsed > timeout:
                        self._timeout_count[name] += 1
                        cb = self._on_timeout_callbacks.get(name)
                        cancel = self._cancel_events.get(name)
                        timed_out.append((name, timeout, elapsed, cb, cancel))

            for name, timeout, elapsed, cb, cancel in timed_out:
                count = self._timeout_count.get(name, 0)
                last = self._last_logged.get(name, 0)
                now_t = time.time()

                # Rate-limit: log first timeout immediately, then every log_interval
                if last == 0 or (now_t - last) >= self._log_interval:
                    self._last_logged[name] = now_t
                    if count <= 1:
                        logger.warning(
                            "Thread %r heartbeat missing for %.0fs (timeout=%.0fs)",
                            name, elapsed, timeout,
                        )
                    else:
                        logger.debug(
                            "Thread %r still timed out after %.0fs (count=%d)",
                            name, elapsed, count,
                        )
                if cb:
                    try:
                        cb(name)
                    except Exception:
                        logger.exception(
                            "Timeout callback failed for %s", name
                        )
                if cancel and not cancel.is_set():
                    logger.warning("Fencing thread %r via cancel event", name)
                    cancel.set()
            time.sleep(interval)

    def fence(self, name: str) -> bool:
        """
        Manually cancel a monitored thread.
        Returns True if a cancel event existed and was set.
        """
        with self._lock:
            cancel = self._cancel_events.get(name)
            if cancel and not cancel.is_set():
                cancel.set()
                logger.warning("Manually fenced thread: %s", name)
                return True
        return False

    def get_status(self) -> dict[str, Any]:
        """Report health of all monitored threads."""
        with self._lock:
            now = time.time()
            status = {}
            for name in self._heartbeats:
                last_hb = self._heartbeats.get(name, 0)
                timeout = self._timeouts.get(name, self._default_timeout)
                elapsed = now - last_hb
                status[name] = {
                    "last_heartbeat": last_hb,
                    "elapsed_seconds": round(elapsed, 1),
                    "timeout_seconds": timeout,
                    "healthy": elapsed < timeout,
                    "timeout_count": self._timeout_count.get(name, 0),
                    "has_cancel_event": name in self._cancel_events,
                }
            return status

    def is_healthy(self, name: str) -> bool:
        """Check if a specific thread is healthy."""
        with self._lock:
            last_hb = self._heartbeats.get(name)
            if last_hb is None:
                return False
            timeout = self._timeouts.get(name, self._default_timeout)
            return (time.time() - last_hb) < timeout
