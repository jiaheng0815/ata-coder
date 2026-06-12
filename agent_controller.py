# -*- coding: utf-8 -*-
"""
Agent Controller — runs CoderAgent on a background thread.

Separates the agent execution from the main/UI thread so that:
- REPL stays responsive during long agent runs
- Ctrl+C / interrupt can cancel the agent without killing the UI
- Events are delivered thread-safely via EventQueue
- Thread health is monitored by ThreadSupervisor
"""

import logging
import queue
import threading
import time
from typing import Any, Optional

from .agent import CoderAgent, CompleteEvent, ErrorEvent
from .config import AppConfig
from .agent_subsystems import AgentSubsystems
from .event_queue import EventQueue
from .thread_supervisor import ThreadSupervisor
from .tools import ToolExecutor
from .sub_agent_manager import SubAgentManager

logger = logging.getLogger(__name__)

__all__ = ["AgentController"]


class AgentController:
    """
    Wraps CoderAgent for background-thread execution.

    Owns:
    - The CoderAgent instance (runs on agent_thread)
    - Input queue (main thread → agent thread)
    - Event queue (agent thread → main thread)
    - ThreadSupervisor (health monitoring)
    - SubAgentManager (created in Phase 5)

    Usage:
        controller = AgentController(config, subsystems, tool_exec)
        controller.start()
        controller.submit("write a hello world script")
        # On the main thread:
        while controller.is_busy():
            for event in controller.event_queue.drain():
                ui.on_event(event)
            time.sleep(0.05)
        controller.shutdown()
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

        # Thread-safe queues
        self.event_queue = EventQueue()
        self._input_queue: queue.Queue = queue.Queue()
        self._cancel = threading.Event()
        self._busy = threading.Event()  # set when task is being processed

        # Thread supervisor — 1800s timeout (LLM calls can be slow)
        self.supervisor = ThreadSupervisor(default_timeout=1800.0)
        self.supervisor.register("agent-main", cancel_event=self._cancel)
        self._heartbeat_thread: Optional[threading.Thread] = None

        # Sub-agent manager (created in Phase 5 integration)
        self._sub_agent_mgr = None

        # Background thread
        self._thread: Optional[threading.Thread] = None
        self._agent: Optional[CoderAgent] = None

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the agent background thread."""
        if self._thread and self._thread.is_alive():
            return

        # Start watchdog
        self.supervisor.start_watchdog(interval=1.0)

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

        # Start agent thread
        self._cancel.clear()
        self._thread = threading.Thread(
            target=self._agent_loop, daemon=True, name="agent-main"
        )
        self._thread.start()

        # Start heartbeat pumper — keeps supervisor happy during long LLM calls
        self._start_heartbeat_pumper()

        logger.info("AgentController started")

    def _start_heartbeat_pumper(self) -> None:
        """Launch a daemon thread that pulses heartbeat while agent is busy."""
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_pumper, daemon=True, name="heartbeat-pumper"
        )
        self._heartbeat_thread.start()

    def _heartbeat_pumper(self) -> None:
        """Pulse heartbeat while agent is busy. Stops instantly on cancel."""
        while not self._cancel.is_set():
            if self._busy.is_set():
                self.supervisor.heartbeat("agent-main")
                # Wait 30s, checking cancel every second
                for _ in range(30):
                    if self._cancel.is_set() or not self._busy.is_set():
                        break
                    time.sleep(1.0)
            else:
                # Idle — wait for a task or cancel
                self._cancel.wait(timeout=5.0)

    def shutdown(self) -> None:
        """Stop the agent thread and cleanup."""
        self._cancel.set()
        self.supervisor.stop_watchdog()
        # Cancel all sub-agents
        if self._sub_agent_mgr:
            self._sub_agent_mgr.shutdown()
            self._sub_agent_mgr = None
        if self._agent:
            try:
                self._agent.shutdown()
            except Exception:
                logger.exception("Agent shutdown error")
            self._agent = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("AgentController shut down")

    # ── Task submission ────────────────────────────────────────────────────

    def submit(
        self,
        task: str,
        skill_name: str | None = None,
        explicit_model: str = "",
        stream: bool = True,
    ) -> None:
        """
        Submit a task for the agent to process.

        If the agent thread is not running, starts it automatically.
        """
        self._busy.set()
        self._input_queue.put({
            "task": task,
            "skill_name": skill_name,
            "explicit_model": explicit_model,
            "stream": stream,
        })

    def cancel(self) -> None:
        """Request cancellation of the current agent run."""
        self._cancel.set()
        logger.info("Agent cancel requested")

    def is_busy(self) -> bool:
        """Check if the agent is currently processing a task (thread-safe)."""
        return self._busy.is_set()

    def is_running(self) -> bool:
        """Check if the agent thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    # ── Sub-agent management ───────────────────────────────────────────────

    def set_sub_agent_manager(self, mgr: Any) -> None:
        """Set the SubAgentManager reference (Phase 5)."""
        self._sub_agent_mgr = mgr
        if self._agent:
            self._agent.set_sub_agent_manager(mgr)

    # ── Health ─────────────────────────────────────────────────────────────

    @property
    def agent(self) -> Optional[CoderAgent]:
        return self._agent

    def health_status(self) -> dict[str, Any]:
        """Return health status of all managed threads."""
        return self.supervisor.get_status()

    # ── Internal ───────────────────────────────────────────────────────────

    def _agent_loop(self) -> None:
        """
        Agent's main thread loop. Waits for tasks from the input queue
        and runs the agent on each one.

        Runs until cancelled. On cancel, drains remaining events,
        shuts down cleanly, and exits.
        """
        while not self._cancel.is_set():
            try:
                # Wait for a task (with timeout so we can check cancel)
                item = self._input_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            task = item.get("task", "")
            skill_name = item.get("skill_name")
            explicit_model = item.get("explicit_model", "")
            stream = item.get("stream", True)

            if not task:
                continue

            try:
                # Heartbeat before starting
                self.supervisor.heartbeat("agent-main")

                logger.info("Agent starting task: %.80s", task)
                result = self._agent.run(
                    task, stream=stream,
                    skill_name=skill_name,
                    explicit_model=explicit_model,
                )
                logger.info("Agent completed task (len=%d)", len(result))

            except Exception as e:
                logger.exception("Agent thread failed on task")
                self.event_queue.put(
                    ErrorEvent(f"Agent error: {e}")
                )
                self.event_queue.put(
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
                self.supervisor.heartbeat("agent-main")

        logger.info("Agent loop exited")
