# -*- coding: utf-8 -*-
"""
Sub-Agent — independent agent with isolated context window.

Each SubAgent runs as an asyncio.Task and has:
- Independent LLM client (separate httpx session)
- Independent message history (no context leakage)
- Independent tool executor (own file cache)
- Cancel support via asyncio.Task.cancel()
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from .config import AppConfig, LLMConfig
# LLMClient/AnthropicClient created via create_llm_client() factory in utils.py
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
        await sub.run("Find the bug in this code: ...")
        result = await sub.wait(timeout=300)
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

        # Independent LLM client — support both OpenAI and Anthropic formats
        llm_config = LLMConfig(
            api_key=config.llm.api_key,
            base_url=config.llm.base_url,
            model=model or config.llm.model,
            temperature=config.llm.temperature,
            max_tokens=config.llm.max_tokens,
            thinking_strength=config.llm.thinking_strength,
            use_anthropic=config.llm.use_anthropic,
        )
        from .utils import create_llm_client
        self._llm, self._use_anthropic = create_llm_client(llm_config)

        # Independent tool executor
        self._tools = ToolExecutor(config.agent)
        self._tool_defs = tools or list(TOOL_DEFINITIONS)
        self._llm.register_tools(self._tool_defs)

        # Safety guard: sub-agents run with the same safety checks as the main
        # agent. Previously, sub-agent tool execution bypassed all safety layers
        # (fool_proof, permissions, privilege), allowing prompt-injected shell
        # commands to execute without any guardrail.
        try:
            from .safety_guard import SafetyGuard
            self._safety = SafetyGuard(config.agent)
        except Exception:
            self._safety = None
            logger.debug("Safety guard unavailable for sub-agent %s", self.id)

        self._messages: list[dict] = []
        self._result: Optional[str] = None
        self._error: Optional[str] = None
        self._tool_call_count = 0
        self._status = "idle"  # idle | running | done | failed | cancelled
        self._event_callback = event_callback
        self._skill_prompt = skill_prompt
        self._task: Optional[asyncio.Task] = None
        self._done = asyncio.Event()

    # ── Public API ───────────────────────────────────────────────────────

    async def run(self, task: str) -> None:
        """Start sub-agent execution as an asyncio.Task."""
        if self._status == "running":
            raise RuntimeError(f"Sub-agent {self.id} is already running")
        self._done.clear()
        self._task = asyncio.create_task(self._run(task))
        self._status = "running"

    async def wait(self, timeout: Optional[float] = 300.0) -> SubAgentResult:
        """Wait until the sub-agent completes or times out."""
        if self._status == "idle":
            return SubAgentResult(agent_id=self.id, result=None,
                                  error="Sub-agent was never started", success=False)
        if not self._task:
            return SubAgentResult(agent_id=self.id, result=self._result,
                                  error=self._error, success=(self._status == "done"),
                                  messages=list(self._messages),
                                  tool_call_count=self._tool_call_count)
        try:
            await asyncio.wait_for(self._done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            await self.cancel()
            return SubAgentResult(
                agent_id=self.id,
                result=self._result,
                error=f"Timeout after {timeout}s",
                success=False,
                tool_call_count=self._tool_call_count,
            )
        return SubAgentResult(
            agent_id=self.id,
            result=self._result,
            error=self._error,
            messages=list(self._messages),
            success=(self._status == "done"),
            tool_call_count=self._tool_call_count,
        )

    async def cancel(self) -> None:
        """Cancel the sub-agent execution."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._status in ("running", "idle"):
            self._status = "cancelled"
        self._done.set()

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

    async def _run(self, task: str) -> None:
        """Internal async target."""
        try:
            self._messages = [
                {"role": "system", "content": self._build_prompt()},
                {"role": "user", "content": task},
            ]
            self._result = await self._loop()
            if self._status == "running":
                self._status = "done"
        except asyncio.CancelledError:
            self._status = "cancelled"
            raise
        except Exception as e:
            logger.exception("Sub-agent %s failed", self.id)
            self._error = str(e)
            self._status = "failed"
        finally:
            try:
                await self._llm.close()
            except Exception:
                logger.debug(
                    "Error closing LLM for sub-agent %s", self.id, exc_info=True
                )
            self._done.set()

    async def _loop(self) -> str:
        """Internal tool-call loop (similar to CoderAgent.run() but simplified)."""
        SAFETY_LIMIT = 50
        last_text = ""

        while self._status == "running":
            if self._tool_call_count >= SAFETY_LIMIT:
                logger.warning("Sub-agent %s reached safety limit", self.id)
                break

            # Anthropic client takes system prompt separately
            if self._use_anthropic:
                sys_msg = next((m.get("content", "") for m in self._messages if m.get("role") == "system"), "")
                response = await self._llm.chat(
                    self._messages, system_prompt=sys_msg, tools=self._tool_defs
                )
            else:
                response = await self._llm.chat(self._messages, tools=self._tool_defs)
            tool_calls = response.get("tool_calls", [])
            text = response.get("content", "")

            if text:
                last_text = text

            if not tool_calls:
                return text or last_text or "Done."

            # Execute tools serially, collecting results
            batch_results: list[tuple[dict, object]] = []
            for tc in tool_calls:
                if self._status != "running":
                    return last_text
                self._tool_call_count += 1
                tool_name = tc["function"]["name"]
                try:
                    arguments = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    arguments = {}

                # Safety check: run through SafetyGuard before executing.
                # Blocks CRITICAL/DANGER operations (rm -rf /, etc.)
                # even in sub-agents, closing the previous safety bypass.
                if self._safety is not None:
                    safety_check = self._safety.check(tool_name, arguments)
                    if not safety_check.allowed:
                        result = ToolResult(
                            success=False, output="",
                            error=f"[Sub-agent blocked] {safety_check.reason}",
                        )
                        batch_results.append((tc, result))
                        logger.warning(
                            "Sub-agent %s blocked: %s → %s",
                            self.id, tool_name, safety_check.reason[:80],
                        )
                        continue

                result = await self._tools.execute(tool_name, arguments)
                batch_results.append((tc, result))

                if self._event_callback:
                    try:
                        self._event_callback(self.id, tool_name, result)
                    except Exception:
                        logger.debug(
                            "Event callback failed for sub-agent %s", self.id,
                            exc_info=True,
                        )

            # Append ONE assistant message with ALL tool_calls (API protocol)
            self._messages.append({
                "role": "assistant",
                "content": text or None,
                "tool_calls": tool_calls,
            })
            for tc, result in batch_results:
                self._messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result.to_message(),
                })

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
