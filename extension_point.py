"""
Thread-safe pub/sub extension point — extracted from ``extension.py``.

ExtensionPoint is the core hook mechanism used by ExtensionManager
and all Extension subclasses.  Handlers are snapshotted under lock
and executed outside the lock to prevent deadlock when a handler
calls register/unregister on the same point.
"""

import logging
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)


class ExtensionPoint:
    """
    Marker for an extension point. Extensions can register callbacks by name.

    Usage:

        # Define an extension point
        ON_SYSTEM_PROMPT = ExtensionPoint("system_prompt")

        # Extension registers a callback
        ON_SYSTEM_PROMPT.register(my_callable)

        # Fire all registered callbacks
        results = ON_SYSTEM_PROMPT.trigger(prompt="...")
    """

    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description
        self._handlers: list[Callable] = []
        self._lock = threading.Lock()

    def register(self, handler: Callable) -> None:
        """注册一个处理器到该扩展点（线程安全）。"""
        with self._lock:
            if handler not in self._handlers:
                self._handlers.append(handler)

    def unregister(self, handler: Callable) -> None:
        """从该扩展点移除一个处理器（线程安全）。"""
        with self._lock:
            try:
                self._handlers.remove(handler)
            except ValueError:
                logger.debug(
                    "Handler not registered in extension point %r", self.name
                )

    def trigger(self, *args: Any, **kwargs: Any) -> list[Any]:
        """
        按注册顺序触发所有处理器（线程安全）。

        在锁内拍快照，锁外执行，避免 handler 内部调用
        register/unregister 造成死锁。

        Returns:
            每个处理器返回值的列表（排除 None）。
        """
        with self._lock:
            handlers = list(self._handlers)
        results = []
        for handler in handlers:
            try:
                result = handler(*args, **kwargs)
                if result is not None:
                    results.append(result)
            except Exception:
                logger.exception(
                    "Extension handler failed for %s: %s", self.name, handler
                )
        return results

    def trigger_first(self, *args: Any, **kwargs: Any) -> Any:
        """
        触发处理器，返回第一个非 None 的结果（线程安全）。
        用于"拦截器"模式：第一个返回非空值的处理器胜出。
        """
        with self._lock:
            handlers = list(self._handlers)
        for handler in handlers:
            try:
                result = handler(*args, **kwargs)
                if result is not None:
                    return result
            except Exception:
                logger.exception(
                    "Extension handler failed for %s: %s", self.name, handler
                )
        return None

    def clear(self) -> None:
        """移除所有处理器（线程安全）。"""
        with self._lock:
            self._handlers.clear()

    def __repr__(self) -> str:
        return f"ExtensionPoint({self.name!r}, handlers={len(self._handlers)})"
