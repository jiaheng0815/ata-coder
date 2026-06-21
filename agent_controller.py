# -*- coding: utf-8 -*-
"""
Agent Controller — runs CoderAgent as an asyncio task.

With the async event loop, the agent runs as a coroutine on the same
thread as the UI. asyncio.Task provides built-in cancellation, and
asyncio.Event replaces threading.Event for coordination.

No more background threads, heartbeat pumpers, or thread supervisors.
"""

import asyncio
import logging
from typing import Any, Optional

from .agent import CoderAgent, CompleteEvent, ErrorEvent
from .config import AppConfig
from .agent_subsystems import AgentSubsystems
from .core.queue import EventQueue
from .tools import ToolExecutor
from .sub_agent_manager import SubAgentManager

logger = logging.getLogger(__name__)

__all__ = ["AgentController"]


class AgentController:
    """
    Wraps CoderAgent for asyncio task execution.

    Owns:
    - The CoderAgent instance (runs as an asyncio.Task)
    - Event queue (agent → UI communication)
    - SubAgentManager (for parallel sub-agent execution)

    Usage:
        controller = AgentController(config, subsystems, tool_exec)
        await controller.start()
        await controller.submit("write a hello world script")
        async for event in controller.event_queue:
            ui.on_event(event)
        await controller.shutdown()
    """

    def __init__(
        self,
        config: AppConfig | None = None,
        subsystems: AgentSubsystems | None = None,
        tool_executor: ToolExecutor | None = None,
    ):
        self._config = config or AppConfig.load()
        self._subsystems = subsystems or AgentSubsystems()
        self._tool_exec = tool_executor or ToolExecutor(self._config.agent)

        # Async event queue (agent → UI)
        self.event_queue = EventQueue(maxsize=5000)

        # Async coordination primitives
        self._cancel = asyncio.Event()
        self._busy = asyncio.Event()

        # Sub-agent manager (created on start)
        self._sub_agent_mgr = None

        # Agent and its task
        self._agent: Optional[CoderAgent] = None
        self._agent_task: Optional[asyncio.Task] = None

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Create the agent and prepare for task submission."""
        if self._agent is not None:
            return  # Already started — idempotent

        # Create SubAgentManager
        max_sub = getattr(self._config.agent, "max_sub_agents", 5)
        sub_timeout = getattr(self._config.agent, "sub_agent_timeout", 300.0)
        self._sub_agent_mgr = SubAgentManager(
            self._config,
            max_concurrent=max_sub,
            default_timeout=sub_timeout,
        )

        # Create agent
        self._agent = CoderAgent(
            config=self._config,
            tool_executor=self._tool_exec,
            subsystems=self._subsystems,
        )
        # Wire event queue and sub-agent manager
        self._agent._event_queue = self.event_queue
        self._agent.set_sub_agent_manager(self._sub_agent_mgr)
        self._tool_exec.set_sub_agent_manager(self._sub_agent_mgr)

        self._cancel.clear()
        self._busy.clear()

        logger.info("AgentController started (async)")

    async def shutdown(self) -> None:
        """Stop the agent and cleanup."""
        self._cancel.set()
        # Cancel agent task
        if self._agent_task and not self._agent_task.done():
            self._agent_task.cancel()
            try:
                await self._agent_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Agent task shutdown error")
            self._agent_task = None

        # Cancel all sub-agents
        if self._sub_agent_mgr:
            await self._sub_agent_mgr.shutdown()
            self._sub_agent_mgr = None

        if self._agent:
            try:
                await self._agent.shutdown()
            except Exception:
                logger.exception("Agent shutdown error")
            self._agent = None

        logger.info("AgentController shut down")

    # ── Task submission ────────────────────────────────────────────────────

    async def submit(
        self,
        task: str,
        skill_name: str | None = None,
        explicit_model: str = "",
        stream: bool = True,
    ) -> None:
        """
        Submit a task for the agent to process.

        Creates an asyncio.Task that runs the agent coroutine.
        If the agent is not started, starts it automatically.
        """
        if not self._agent:
            await self.start()

        # Cancel any still-running agent task to prevent leaks (server mode)
        if self._agent_task and not self._agent_task.done():
            logger.warning("Cancelling previous agent task before starting new one")
            self._agent_task.cancel()
            try:
                await self._agent_task
            except (asyncio.CancelledError, Exception):
                pass

        self._busy.set()
        self._cancel.clear()

        async def _agent_runner():
            """Run the agent task with proper error handling."""
            try:
                logger.info("Agent starting task: %.80s", task)
                result = await self._agent.run(
                    task, stream=stream,
                    skill_name=skill_name,
                    explicit_model=explicit_model,
                )
                logger.info("Agent completed task (len=%d)", len(result))
            except asyncio.CancelledError:
                logger.info("Agent task cancelled")
                raise
            except Exception:
                logger.exception("Agent task failed")
                # Sanitize — full details are in the log; never leak exception
                # messages to the event stream.
                await self.event_queue.put(
                    ErrorEvent("An unexpected error occurred. Check logs for details.")
                )
                await self.event_queue.put(
                    CompleteEvent(
                        total_tool_calls=(
                            self._agent._state.tool_call_count
                            if self._agent else 0
                        ),
                        total_time=0,
                    )
                )
            finally:
                self._busy.clear()

        self._agent_task = asyncio.create_task(_agent_runner())

    async def cancel(self) -> None:
        """Request cancellation of the current agent run."""
        self._cancel.set()
        if self._agent_task and not self._agent_task.done():
            self._agent_task.cancel()
        logger.info("Agent cancel requested")

    def is_busy(self) -> bool:
        """Check if the agent is currently processing a task."""
        return self._busy.is_set()

    def is_running(self) -> bool:
        """Check if the agent task is active."""
        return self._agent_task is not None and not self._agent_task.done()

    # ── Sub-agent management ───────────────────────────────────────────────

    def set_sub_agent_manager(self, mgr: Any) -> None:
        """Set the SubAgentManager reference."""
        self._sub_agent_mgr = mgr
        if self._agent:
            self._agent.set_sub_agent_manager(mgr)

    # ── Health ─────────────────────────────────────────────────────────────

    @property
    def agent(self) -> Optional[CoderAgent]:
        return self._agent

    def health_status(self) -> dict[str, Any]:
        """Return health status (simplified — no threads to monitor)."""
        return {
            "agent": "running" if self.is_running() else "idle",
            "busy": self._busy.is_set(),
            "sub_agents": len(self._sub_agent_mgr.list_active()) if self._sub_agent_mgr else 0,
        }
