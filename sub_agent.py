# -*- coding: utf-8 -*-
"""
Sub-Agent — independent agent with isolated context window.

Each SubAgent runs in its own daemon thread and has:
- Independent LLM client (separate httpx session)
- Independent message history (no context leakage)
- Independent tool executor (own file cache)
- Cancel support via threading.Event

The main agent can spawn multiple sub-agents for parallel work.
"""

import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .config import AppConfig, LLMConfig
from .llm_client import LLMClient
from .tools import ToolExecutor, TOOL_DEFINITIONS, ToolResult

logger = logging.getLogger(__name__)

__all__ = ["SubAgent", "SubAgentResult"]


@dataclass
class SubAgentResult:
    """Result returned when a sub-agent completes."""
    agent_id: str
    result: Optional[str] = None
    error: Optional[str] = None
    messages: list[dict] = field(default_factory=list)
    success: bool = True
    tool_call_count: int = 0


class SubAgent:
    """
    Independent sub-agent with isolated context window.

    Usage:
        sub = SubAgent(config=config, skill_prompt="You are a debugger...")
        sub.run("Find the bug in this code: ...")
        # On the main thread:
        result = sub.wait(timeout=300)
        print(result.result)
    """

    def __init__(
        self,
        config: AppConfig,
        skill_prompt: str = "",
        model: Optional[str] = None,
        tools: Optional[list[dict]] = None,
        event_callback: Optional[Callable[[str, str, ToolResult], None]] = None,
        agent_id: Optional[str] = None,
    ):
        self.id = agent_id or f"sub_{uuid.uuid4().hex[:8]}"
        self._cancel = threading.Event()
        self._done = threading.Event()

        # Independent LLM client
        llm_config = LLMConfig(
            api_key=config.llm.api_key,
            base_url=config.llm.base_url,
            model=model or config.llm.model,
            temperature=config.llm.temperature,
            max_tokens=config.llm.max_tokens,
        )
        self._llm = LLMClient(llm_config)

        # Independent tool executor
        self._tools = ToolExecutor(config.agent)
        self._tool_defs = tools or list(TOOL_DEFINITIONS)
        self._llm.register_tools(self._tool_defs)

        self._messages: list[dict] = []
        self._result: Optional[str] = None
        self._error: Optional[str] = None
        self._tool_call_count = 0
        self._status = "idle"  # idle | running | done | failed | cancelled
        self._event_callback = event_callback
        self._skill_prompt = skill_prompt
        self._thread: Optional[threading.Thread] = None

    # ── Public API ───────────────────────────────────────────────────────

    def run(self, task: str) -> None:
        """Start sub-agent execution in a background thread."""
        if self._status == "running":
            raise RuntimeError(f"Sub-agent {self.id} is already running")
        self._cancel.clear()
        self._done.clear()
        self._status = "running"
        self._thread = threading.Thread(
            target=self._run_target, args=(task,),
            daemon=True, name=f"subagent-{self.id}"
        )
        self._thread.start()

    def wait(self, timeout: Optional[float] = 300.0) -> SubAgentResult:
        """Block until the sub-agent completes or times out."""
        if self._status == "idle":
            return SubAgentResult(agent_id=self.id, result=None,
                                  error="Sub-agent was never started", success=False)
        if self._done.wait(timeout=timeout):
            return SubAgentResult(
                agent_id=self.id,
                result=self._result,
                error=self._error,
                messages=list(self._messages),
                success=(self._status == "done"),
                tool_call_count=self._tool_call_count,
            )
        # Timeout
        self.cancel()
        return SubAgentResult(
            agent_id=self.id,
            result=self._result,
            error=f"Timeout after {timeout}s",
            success=False,
            tool_call_count=self._tool_call_count,
        )

    def cancel(self) -> None:
        """Cancel the sub-agent execution and release LLM resources."""
        self._cancel.set()
        if self._status in ("running", "idle"):
            self._status = "cancelled"
        self._done.set()
        # Close LLM client regardless of whether thread started
        try:
            self._llm.close()
        except Exception:
            logger.warning(
                "Failed to close LLM for sub-agent %s", self.id, exc_info=True
            )

    def is_running(self) -> bool:
        return self._status == "running"

    def is_done(self) -> bool:
        return self._status in ("done", "failed", "cancelled")

    @property
    def status(self) -> str:
        return self._status

    @property
    def result(self) -> Optional[str]:
        return self._result

    @property
    def messages(self) -> list[dict]:
        return list(self._messages)

    @property
    def tool_call_count(self) -> int:
        return self._tool_call_count

    # ── Internal ─────────────────────────────────────────────────────────

    def _run_target(self, task: str) -> None:
        """Internal thread target."""
        try:
            self._messages = [
                {"role": "system", "content": self._build_prompt()},
                {"role": "user", "content": task},
            ]
            self._result = self._loop()
            if self._status == "running":
                self._status = "done"
        except Exception as e:
            logger.exception("Sub-agent %s failed", self.id)
            self._error = str(e)
            self._status = "failed"
        finally:
            try:
                self._llm.close()
            except Exception:
                logger.debug(
                    "Error closing LLM for sub-agent %s", self.id, exc_info=True
                )
            self._done.set()

    def _loop(self) -> str:
        """Internal tool-call loop (similar to CoderAgent.run() but simplified)."""
        SAFETY_LIMIT = 50
        last_text = ""

        while not self._cancel.is_set():
            if self._tool_call_count >= SAFETY_LIMIT:
                logger.warning("Sub-agent %s reached safety limit", self.id)
                break

            response = self._llm.chat(self._messages, tools=self._tool_defs)
            tool_calls = response.get("tool_calls", [])
            text = response.get("content", "")

            if text:
                last_text = text

            if not tool_calls:
                return text or last_text or "Done."

            # Execute tools serially
            for tc in tool_calls:
                if self._cancel.is_set():
                    return last_text
                self._tool_call_count += 1
                tool_name = tc["function"]["name"]
                try:
                    arguments = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    arguments = {}

                result = self._tools.execute(tool_name, arguments)

                self._messages.append({
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": [tc],
                })
                self._messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result.to_message(),
                })

                if self._event_callback:
                    try:
                        self._event_callback(self.id, tool_name, result)
                    except Exception:
                        logger.debug(
                            "Event callback failed for sub-agent %s", self.id,
                            exc_info=True,
                        )

        return last_text

    def _build_prompt(self) -> str:
        """Build the sub-agent's system prompt."""
        parts = []
        if self._skill_prompt:
            parts.append(self._skill_prompt)
        parts.append(
            "You are a sub-agent working on a delegated task. "
            "Complete your task independently and return a clear result. "
            "Do not ask follow-up questions — the main agent cannot see them."
        )
        return "\n\n".join(parts)
