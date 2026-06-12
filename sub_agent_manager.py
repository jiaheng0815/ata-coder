# -*- coding: utf-8 -*-
"""
SubAgentManager — lifecycle management for concurrent sub-agents.

Handles spawning, collecting, cancelling, and listing sub-agents.
Enforces concurrency limits to prevent resource exhaustion.
"""

import logging
import queue
import threading
import uuid
from typing import Any, Callable, Optional

from .config import AppConfig
from .sub_agent import SubAgent, SubAgentResult

logger = logging.getLogger(__name__)

__all__ = ["SubAgentManager"]


class SubAgentManager:
    """
    Manages sub-agent lifecycle: spawn, collect, cancel, list.

    Usage:
        mgr = SubAgentManager(config, max_concurrent=5)
        aid = mgr.spawn("Search for all TODO comments", skill_prompt="...")
        # ... do other work ...
        result = mgr.collect(aid, timeout=300)
        print(result.result)
    """

    def __init__(
        self,
        config: AppConfig,
        max_concurrent: int = 5,
        default_timeout: float = 300.0,
    ):
        self._config = config
        self._max_concurrent = max_concurrent
        self._default_timeout = default_timeout
        self._agents: dict[str, SubAgent] = {}
        self._lock = threading.Lock()

    # ── Spawn ──────────────────────────────────────────────────────────

    def spawn(
        self,
        task: str,
        skill_prompt: str = "",
        model: Optional[str] = None,
        tools: Optional[list[dict]] = None,
        event_callback: Optional[Callable] = None,
    ) -> str:
        """
        Spawn a sub-agent. Returns the agent_id.

        Args:
            task: The task to delegate (must be self-contained)
            skill_prompt: Optional skill system prompt for the sub-agent
            model: Optional model override
            tools: Optional tool list (defaults to built-in tools)
            event_callback: Optional callback for tool results

        Returns:
            agent_id string

        Raises:
            RuntimeError: if max concurrent agents reached
        """
        with self._lock:
            running = sum(
                1 for a in self._agents.values() if a.is_running()
            )
            if running >= self._max_concurrent:
                raise RuntimeError(
                    f"Max concurrent sub-agents ({self._max_concurrent}) "
                    f"reached. Wait for some to complete or increase the limit."
                )

            agent_id = f"sub_{uuid.uuid4().hex[:8]}"
            sub = SubAgent(
                config=self._config,
                skill_prompt=skill_prompt,
                model=model,
                tools=tools,
                event_callback=event_callback,
                agent_id=agent_id,
            )
            self._agents[agent_id] = sub
            # Count before releasing lock (prevents TOCTOU race)
            running_after = running + 1

        # sub.run() starts a daemon thread and returns immediately — safe outside lock
        sub.run(task)
        logger.info("Sub-agent spawned: %s (running=%d)", agent_id, running_after)
        return agent_id

    # ── Collect ────────────────────────────────────────────────────────

    def collect(self, agent_id: str, timeout: Optional[float] = None) -> SubAgentResult:
        """Wait for and collect a sub-agent's result."""
        if timeout is None:
            timeout = self._default_timeout
        sub = self._agents.get(agent_id)
        if not sub:
            return SubAgentResult(
                agent_id=agent_id, result=None,
                error=f"Unknown agent: {agent_id}", success=False,
            )
        return sub.wait(timeout=timeout)

    def collect_all(self, timeout: Optional[float] = None) -> list[SubAgentResult]:
        """Collect results from all sub-agents."""
        return [
            self.collect(aid, timeout)
            for aid in list(self._agents.keys())
        ]

    # ── Cancel ─────────────────────────────────────────────────────────

    def cancel(self, agent_id: str) -> bool:
        """Cancel a specific sub-agent."""
        sub = self._agents.get(agent_id)
        if sub and sub.is_running():
            sub.cancel()
            return True
        return False

    def cancel_all(self) -> None:
        """Cancel all running sub-agents."""
        with self._lock:
            for sub in self._agents.values():
                if sub.is_running():
                    sub.cancel()

    # ── Queries ────────────────────────────────────────────────────────

    def get(self, agent_id: str) -> Optional[SubAgent]:
        """Get a sub-agent by ID."""
        return self._agents.get(agent_id)

    def list_all(self) -> list[SubAgent]:
        """List all sub-agents."""
        with self._lock:
            return list(self._agents.values())

    def list_active(self) -> list[SubAgent]:
        """List only running sub-agents."""
        with self._lock:
            return [a for a in self._agents.values() if a.is_running()]

    def list_finished(self) -> list[SubAgent]:
        """List only completed/failed/cancelled sub-agents."""
        with self._lock:
            return [a for a in self._agents.values() if a.is_done()]

    @property
    def active_count(self) -> int:
        with self._lock:
            return sum(1 for a in self._agents.values() if a.is_running())

    @property
    def total_count(self) -> int:
        with self._lock:
            return len(self._agents)

    # ── Cleanup ────────────────────────────────────────────────────────

    def clear_finished(self) -> int:
        """Remove finished agents from tracking. Returns count removed."""
        with self._lock:
            to_remove = [
                aid for aid, a in self._agents.items()
                if a.is_done()
            ]
            for aid in to_remove:
                del self._agents[aid]
            return len(to_remove)

    def shutdown(self) -> None:
        """Cancel all agents and cleanup."""
        self.cancel_all()
        self._agents.clear()
