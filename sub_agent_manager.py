# -*- coding: utf-8 -*-
"""
SubAgentManager — lifecycle management for concurrent sub-agents.

Handles spawning, collecting, cancelling, and listing sub-agents.
Uses asyncio.Semaphore for concurrency limits.
"""

import asyncio
import logging
import uuid
from typing import Callable, Optional

from .config import AppConfig
from .sub_agent import SubAgent, SubAgentResult

logger = logging.getLogger(__name__)

__all__ = ["SubAgentManager"]


class SubAgentManager:
    """
    Manages sub-agent lifecycle: spawn, collect, cancel, list.

    Usage:
        mgr = SubAgentManager(config, max_concurrent=5)
        aid = await mgr.spawn("Search for all TODO comments", skill_prompt="...")
        # ... do other work ...
        result = await mgr.collect(aid, timeout=300)
        print(result.result)
    """

    def __init__(
        self,
        config: AppConfig,
        max_concurrent: int = 5,
        default_timeout: float = 300.0,
    ):
        self._config = config
        self._default_timeout = default_timeout
        self._agents: dict[str, SubAgent] = {}
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._collected: set[str] = set()  # track agents already released via collect()
        self._collecting: set[str] = set()  # agents currently inside collect() (prevents clear_finished double-release)

    # ── Spawn ──────────────────────────────────────────────────────────

    async def spawn(
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
        # Acquire semaphore slot with timeout (avoids TOCTOU + private attr access)
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=30.0)
        except asyncio.TimeoutError:
            running = sum(1 for a in self._agents.values() if a.is_running())
            raise RuntimeError(
                f"Max concurrent sub-agents reached ({running} running). "
                f"Wait for some to complete or increase the limit."
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

        try:
            await sub.run(task)
        except Exception:
            # Release semaphore on failure — caller may never call collect()
            self._semaphore.release()
            raise
        running = sum(1 for a in self._agents.values() if a.is_running())
        logger.info("Sub-agent spawned: %s (running=%d)", agent_id, running)
        return agent_id

    # ── Collect ────────────────────────────────────────────────────────

    async def collect(self, agent_id: str, timeout: Optional[float] = None) -> SubAgentResult:
        """Wait for and collect a sub-agent's result."""
        if timeout is None:
            timeout = self._default_timeout
        sub = self._agents.get(agent_id)
        if not sub:
            return SubAgentResult(
                agent_id=agent_id, result=None,
                error=f"Unknown agent: {agent_id}", success=False,
            )
        # Mark as collecting BEFORE any await — prevents clear_finished()
        # from releasing the semaphore slot between sub.wait() and _collected check.
        self._collecting.add(agent_id)
        try:
            try:
                result = await sub.wait(timeout=timeout)
            except (Exception, asyncio.CancelledError):
                # Release semaphore slot on failure too, to avoid permanent leak.
                # CancelledError inherits from BaseException, not Exception —
                # must be listed explicitly.
                if agent_id not in self._collected:
                    self._semaphore.release()
                    self._collected.add(agent_id)
                raise
            # Release semaphore slot only once per agent
            if agent_id not in self._collected:
                self._semaphore.release()
                self._collected.add(agent_id)
        finally:
            self._collecting.discard(agent_id)
        return result

    async def collect_all(self, timeout: Optional[float] = None) -> list[SubAgentResult]:
        """Collect results from all sub-agents. Survives individual failures."""
        results = []
        for aid in list(self._agents.keys()):
            try:
                results.append(await self.collect(aid, timeout))
            except Exception:
                logger.exception("Failed to collect sub-agent %s", aid)
                results.append(SubAgentResult(
                    agent_id=aid, result=None,
                    error=f"Collection failed for {aid}", success=False,
                ))
        return results

    # ── Cancel ─────────────────────────────────────────────────────────

    async def cancel(self, agent_id: str) -> bool:
        """Cancel a specific sub-agent."""
        sub = self._agents.get(agent_id)
        if sub and sub.is_running():
            await sub.cancel()
            return True
        return False

    async def cancel_all(self) -> None:
        """Cancel all running sub-agents."""
        for sub in list(self._agents.values()):
            if sub.is_running():
                await sub.cancel()

    # ── Queries ────────────────────────────────────────────────────────

    def get(self, agent_id: str) -> Optional[SubAgent]:
        """Get a sub-agent by ID."""
        return self._agents.get(agent_id)

    def list_all(self) -> list[SubAgent]:
        """List all sub-agents."""
        return list(self._agents.values())

    def list_active(self) -> list[SubAgent]:
        """List only running sub-agents."""
        return [a for a in self._agents.values() if a.is_running()]

    def list_finished(self) -> list[SubAgent]:
        """List only completed/failed/cancelled sub-agents."""
        return [a for a in self._agents.values() if a.is_done()]

    @property
    def active_count(self) -> int:
        return sum(1 for a in self._agents.values() if a.is_running())

    @property
    def total_count(self) -> int:
        return len(self._agents)

    # ── Cleanup ────────────────────────────────────────────────────────

    def clear_finished(self) -> int:
        """Remove finished agents from tracking. Returns count removed.

        Releases one semaphore slot per removed agent that was NOT already
        collected via :meth:`collect` and is NOT currently inside a
        :meth:`collect` call (which would cause a double-release).
        """
        to_remove = [
            aid for aid, a in self._agents.items()
            if a.is_done()
        ]
        for aid in to_remove:
            del self._agents[aid]
            if aid not in self._collected and aid not in self._collecting:
                self._semaphore.release()
            else:
                self._collected.discard(aid)
        if to_remove:
            logger.debug("Cleared %d finished sub-agent(s) (slots released)", len(to_remove))
        return len(to_remove)

    async def shutdown(self) -> None:
        """Cancel all agents and wait for them to finish."""
        await self.cancel_all()
        # Wait for all tasks to finish
        for agent in list(self._agents.values()):
            if agent._task and not agent._task.done():
                try:
                    await asyncio.wait_for(agent._task, timeout=5.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
            # Release semaphore slot for every agent (shutdown is terminal).
            # Skip agents currently inside collect() — they own their release.
            if agent.id not in self._collected and agent.id not in self._collecting:
                self._semaphore.release()
                self._collected.add(agent.id)
        self._agents.clear()
