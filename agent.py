# -*- coding: utf-8 -*-
"""
Core Agent loop for ATA Coder.

Integrates:
- Skills system (configurable personas)
- Memory system (persistent context across sessions)
- MCP client (cross-system tool interoperability)
- Prompt templates (dynamic context injection)
- Permission system (interactive allow/deny)
- Project detection (language, framework, build system)
- Session persistence (save/resume/export)

The agent runs a conversation loop:
1. Build system prompt from skill + memory + templates + project context
2. Send conversation to the LLM
3. Execute tool calls (built-in + MCP) with permission checks
4. Feed results back and continue
5. Complete when the task is done
"""

import asyncio
import json
import logging
import time
from typing import Any, Callable

from .config import AppConfig
from .llm_client import SYSTEM_PROMPT
from .tools import ToolExecutor, TOOL_DEFINITIONS, ToolResult
from .types import Message
from .agent_subsystems import AgentSubsystems
from .system_prompt_builder import SystemPromptBuilder
from .fool_proof import FoolProofEngine
from .change_tracker import ChangeTracker
from .privilege import PrivilegeManager

from .self_correct import SelfCorrectionEngine
from .git_workflow import GitWorkflow
from .extension import get_extension_manager
from .clawd_integration import get_clawd
from .agent_compact import CompactionMixin
from .agent_tools import ToolExecutionMixin
from .agent_routing import ModelRoutingMixin
from .agent_extension import ExtensionMixin
from .agent_session import AgentSessionMixin
from .context_manager import ContextManager

# ── Event types & Agent state ──────────────────────────────────────────
from .core import (  # noqa: F401 — re-exported for external use
    AgentEvent, CompleteEvent, ErrorEvent, MemorySuggestionEvent,
    ReasoningEvent, SkillChangedEvent, TextDeltaEvent,
    ThinkingEvent, ToolCallEvent, ToolResultEvent, ToolStreamEvent,
)
from .core.state import AgentState, AgentPhase

logger = logging.getLogger(__name__)


class _SessionLogger(logging.LoggerAdapter):
    """Injects ``session_id`` into log records for structured tracing."""

    def process(self, msg, kwargs):
        sid = self.extra.get("session_id", "") if self.extra else ""
        if sid:
            return f"[{sid[:8]}] {msg}", kwargs
        return msg, kwargs


# ── The Agent ────────────────────────────────────────────────────────────────

class CoderAgent(CompactionMixin, ToolExecutionMixin,
                 ModelRoutingMixin, ExtensionMixin, AgentSessionMixin):
    """
    The main ATA Coder agent with skills, memory, MCP, templates,
    permissions, project detection, and session persistence.
    """

    def __init__(
        self,
        config: AppConfig | None = None,
        tool_executor: ToolExecutor | None = None,
        subsystems: AgentSubsystems | None = None,
    ):
        self.config = config or AppConfig.load()

        # Choose client: Anthropic or OpenAI format (factory eliminates duplication)
        from .utils import create_llm_client
        self.llm, self._use_anthropic = create_llm_client(self.config.llm)

        self.tools = tool_executor or ToolExecutor(self.config.agent)

        # ── Subsystems ────────────────────────────────────────────────────
        self.subsys = subsystems or AgentSubsystems()
        self.skills = self.subsys.skills
        self.memory = self.subsys.memory
        self.mcp = self.subsys.mcp
        if self.mcp:
            self.tools.set_mcp_client(self.mcp)
        self.templates = self.subsys.templates
        self.permissions = self.subsys.permissions
        self.project_info = self.subsys.project_info
        self.sessions = self.subsys.sessions

        # ── Extension Manager ─────────────────────────────────────────────
        if self.subsys.extensions is not None:
            self.ext_mgr = self.subsys.extensions
        else:
            self.ext_mgr = get_extension_manager()
            self.subsys.extensions = self.ext_mgr

        # Register skills as extensions
        self._register_skills_as_extensions()

        # Discover extensions from extension directories
        self._discover_extensions()

        # Register extension points for agent lifecycle hooks
        self._register_extension_points()

        # Activate all skill-tagged extensions (multi-skill)
        for ext_name in [e.meta.name for e in self.ext_mgr.get_by_tag("skill")]:
            self.ext_mgr.activate(ext_name)

        # ── System prompt builder ─────────────────────────────────────────
        self._prompt_builder = SystemPromptBuilder(
            subsystems=self.subsys,
            workspace_dir=self.tools.workspace,
            model=self.config.llm.model,
            default_prompt=SYSTEM_PROMPT,
        )

        # ── Tool & safety infrastructure ──────────────────────────────────
        self.change_tracker = ChangeTracker()
        self.fool_proof = FoolProofEngine(
            workspace=self.tools.workspace,
            permission_store=self.permissions,
            change_tracker=self.change_tracker,
        )

        self.privilege_mgr = PrivilegeManager(self.tools.workspace)
        self.self_correct = SelfCorrectionEngine(max_retries=1)
        self.git = GitWorkflow(self.tools.workspace)

        # Per-instance self-correction depth (was a class variable shared across
        # all agent instances — dangerous under ThreadingHTTPServer in server mode).
        self._self_correct_depth: int = 0

        self._state = AgentState()
        self._on_event: Callable[[AgentEvent], None] | None = None
        self._current_session_id: str = ""
        self._pending_memory_suggestions: list[str] = []
        self._cached_system_prompt: str | None = None  # invalidated on new build / compact
        self._cached_allowed_tools: set[str] | None = None  # invalidated on skill change

        # ── Context manager (O(1) token tracking, segment-split, adaptive compact) ──
        self._context_manager = ContextManager(self.config.agent)
        self._summary_llm = None  # lazily created summarisation client

        # Build the combined tool list
        self._all_tools = list(TOOL_DEFINITIONS)
        if self.mcp:
            mcp_tools = self.mcp.get_tools()
            self._all_tools.extend(mcp_tools)
            logger.debug(
                "MCP tools (before connect): %d — servers connect in run()",
                len(mcp_tools),
            )

        # Extension tools
        ext_tools = self.ext_mgr.aggregate_tools()
        if ext_tools:
            self._all_tools.extend(ext_tools)
            logger.debug("Extension tools added: %d", len(ext_tools))

        logger.debug(
            "Total tools: %d builtin + %s MCP + %s extensions = %d",
            len(TOOL_DEFINITIONS),
            len(mcp_tools) if self.mcp else 0,
            len(ext_tools),
            len(self._all_tools),
        )

        self.llm.register_tools(self._all_tools)

        # ── Sub-agent manager (set later by AgentController if used) ──────
        self._sub_agent_mgr = None

        # ── Parallel tool execution uses asyncio.gather ───────────────────

    # ── Model routing → agent_routing.py (ModelRoutingMixin)
    # ── Extension management → agent_extension.py (ExtensionMixin)

    # ── Event system ──────────────────────────────────────────────────────

    def on_event(self, callback: Callable[[AgentEvent], None]) -> None:
        self._on_event = callback

    def _emit(self, event: AgentEvent) -> None:
        """Emit event to both callback and EventQueue (if available).

        Uses put_nowait() for non-blocking FIFO — safe to call from
        both asyncio tasks and asyncio.to_thread() contexts.
        """
        event_queue = getattr(self, "_event_queue", None)
        if event_queue is not None:
            try:
                event_queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("Event queue full (%d pending), dropping event: %s",
                              event_queue.count(), type(event).__name__)
            except Exception:
                logger.debug("Event queue closed — dropping event: %s", type(event).__name__)
        # Backward-compatible callback
        if self._on_event:
            self._on_event(event)

    # ── Main entry point ──────────────────────────────────────────────────

    async def run(self, task: str, stream: bool = True, skill_name: str | None = None,
            explicit_model: str = "", reset_context: bool = True) -> str:
        """
        Run the agent on a given task.

        Args:
            task: User task description
            stream: Enable streaming output
            skill_name: Force a specific skill (or None for auto-detect)
            explicit_model: Explicit model override (bypasses auto-routing)
            reset_context: If False, preserve existing conversation history
                           (for persistent sessions like the HTTP API).

        Returns:
            Final response text
        """
        # ── Connect pending MCP servers (deferred from __init__) ───────────
        if self.mcp:
            await self.mcp.connect_all()
            # Refresh tool list: __init__ ran before servers were connected,
            # so get_tools() returned empty. After connect_all() the tools
            # are discovered — re-fetch and re-register with the LLM.
            mcp_tools = self.mcp.get_tools()
            if mcp_tools:
                # Rebuild: keep built-in + extension tools, replace MCP tools
                self._all_tools = [t for t in self._all_tools if not t.get("function", {}).get("name", "").startswith("mcp__")]
                self._all_tools.extend(mcp_tools)
                self.llm.register_tools(self._all_tools)
                logger.info("MCP tools refreshed after connect: %d tools", len(mcp_tools))

        # ── Persistent session: preserve existing conversation ─────────────
        if not reset_context and self._state.messages:
            # Append new user message to existing conversation; keep system
            # prompt and all prior messages intact.
            self._append_message({"role": "user", "content": task})
            # Rebuild system prompt for updated memory context but don't
            # replace the original system message (memory/git context may
            # have changed, but conversation integrity is paramount).
            system_prompt = self._build_system_prompt(task)
            self._cached_system_prompt = system_prompt
            self._cached_allowed_tools = None
            self._state.tool_call_count = 0  # reset per-run counter
            logger.info("Agent run (session): skill=%s, model=%s, session=%s, "
                         "history=%d msgs, task=%.100s",
                         self.skills.active_skill.name if self.skills and self.skills.active_skill else "default",
                         self.current_model,
                         self._current_session_id,
                         len(self._state.messages),
                         task)
        else:
            self._state = AgentState(start_time=time.time(), phase=AgentPhase.INITIALIZING)
            self._pending_memory_suggestions = []  # reset for fresh context

            # ── Model routing ──────────────────────────────────────────────
            self._route_for_task(task, explicit_model)

            # Trigger extension point: on_model_route
            self._ep_on_model_route.trigger(
                task=task, complexity=self._routed_complexity, model=self.current_model
            )

            # Trigger extension point: on_run_start
            self._ep_on_run_start.trigger(task=task, skill_name=skill_name)

            # Reset change tracker for new run
            self.change_tracker.reset()
            self.change_tracker.dry_run = False

            # Generate session ID
            from .session import generate_session_id
            self._current_session_id = generate_session_id(
                task,
                skill_name or (self.skills.active_skill.name if self.skills and self.skills.active_skill else ""),
            )
            # Per-session structured logger — injects session_id prefix
            self._slog = _SessionLogger(logger, {"session_id": self._current_session_id})

            # Skill selection (multi-skill support)
            if skill_name and self.skills:
                skill = self.skills.activate(skill_name, merge=True)
                if skill:
                    self._emit(SkillChangedEvent(skill.name))
            elif self.skills:
                # Keyword-based skill detection — zero extra LLM calls.
                # Single-skill activation only: multi-skill merging causes
                # confusion with weaker models (prompt dilution).
                detected = self.skills.detect_skill(task)
                if detected and detected.name != "general-coder":
                    self.skills.activate(detected.name, merge=False)
                    self._emit(SkillChangedEvent(detected.name))
                    logger.info(
                        "Skill route: %s for: %.80s", detected.name, task
                    )

            # Build system prompt — pass the user task for targeted memory recall
            system_prompt = self._build_system_prompt(task)
            self._cached_system_prompt = system_prompt  # pre-seed cache
            self._cached_allowed_tools = None  # invalidate on new run

            initial_msgs = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]
            self._state.messages = initial_msgs
            self._context_manager.replace_all(initial_msgs)

            logger.info("Agent run: skill=%s, model=%s, session=%s, task=%.100s",
                         self.skills.active_skill.name if self.skills and self.skills.active_skill else "default",
                         self.current_model,
                         self._current_session_id,
                         task)

        # ── Main agent loop with error boundary ──────────────────────────────
        try:
            return await self._run_loop(task, stream)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except Exception as e:
            logger.critical("Agent fatal error: %s", e, exc_info=True)
            # Sanitize — full details are in the log; never leak exception
            # messages (which may contain paths / keys) to the caller.
            self._emit(ErrorEvent("An unexpected error occurred. Check logs for details."))
            return "An unexpected error occurred. Please check the logs for details."
        finally:
            self._state.phase = AgentPhase.SHUTDOWN
            # Auto-save session after every task (best-effort, never crashes)
            self._auto_save_session()
            # Deactivate skill only for fresh-context runs; persistent
            # (reset_context=False) sessions keep their skill active.
            if self.skills and reset_context:
                self.skills.deactivate()

    async def _run_loop(self, task: str, stream: bool = True) -> str:
        """Main agent loop — extracted for error boundary isolation."""
        SAFETY_LIMIT = 999  # circuit breaker — not a tool-call "limit"
        _consecutive_failures = 0  # break loop when model is stuck failing
        _MAX_CONSECUTIVE_FAILURES = 5
        last_text = ""  # guard against UnboundLocalError when LLM returns empty content
        final_response = ""  # guard against UnboundLocalError on safety-limit / failure break
        while True:
            # Circuit breaker: prevent infinite loop when the model keeps
            # emitting tool calls (hallucination / API bug).  This is NOT a
            # user-facing tool limit — just a last-resort safety net.
            if self._state.tool_call_count >= SAFETY_LIMIT:
                self._state.phase = AgentPhase.ERROR
                self._state.safety_limit_reached = True
                logger.critical(
                    "SAFETY_LIMIT reached: %d tool calls. Breaking loop.",
                    self._state.tool_call_count,
                )
                self._emit(ErrorEvent(
                    f"Safety limit reached ({SAFETY_LIMIT} tool calls). "
                    "The model may be stuck in a tool-call loop."
                ))
                # Clawd: error state — prevent stuck thinking animation
                get_clawd().error(
                    f"Safety limit reached ({SAFETY_LIMIT} tool calls)"
                )
                break

            self._state.phase = AgentPhase.THINKING
            self._emit(ThinkingEvent())

            # Clawd: model is generating, show thinking animation
            get_clawd().thinking()

            # Auto-compact when approaching the effective context limit (O(1) check).
            if self._context_manager.should_compact():
                est = self._context_manager.token_total
                max_t = self.config.agent.max_context_tokens
                logger.warning("Token budget: %d/%d effective (%.0f%% of %d max), auto-compacting",
                             est, self.config.agent.effective_context_tokens,
                             est / max(max_t, 1) * 100, max_t)
                await self.compact()
            # Hard ceiling: if compaction didn't help enough, force-truncate
            if self._context_manager.needs_force_truncate():
                logger.critical("Hard token ceiling: %d > 95%% of %d max. Force-truncating.",
                               self._context_manager.token_total,
                               self.config.agent.max_context_tokens)
                self._force_truncate()

            # Get allowed tools from multi-skill intersection
            allowed_tool_names = self._compute_allowed_tools()

            filtered_tools = self._all_tools
            if allowed_tool_names is not None and len(allowed_tool_names) > 0:
                filtered_tools = [
                    t for t in self._all_tools
                    if t["function"]["name"] in allowed_tool_names
                    or t["function"]["name"].startswith("mcp__")
                ]

            if stream:
                response = await self._streaming_chat(filtered_tools)
            else:
                response = await self.llm.chat(
                    self._state.messages,
                    tools=filtered_tools,
                    system_prompt=self._extract_system_prompt(),
                )

            tool_calls = response.get("tool_calls", [])
            text = response.get("content", "")

            if text:
                last_text = text

            if not tool_calls:
                final_response = text or last_text
                self._state.phase = AgentPhase.COMPLETED
                # Clawd: Stop — model finished its turn
                get_clawd().stop(assistant_output=final_response)
                break

            # Pre-parse args for both parallelization check and execution
            pre_parsed: dict[int, dict] = {}
            for i, tc in enumerate(tool_calls):
                try:
                    pre_parsed[i] = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    pre_parsed[i] = {}
            batch_results: list[ToolResult] = []

            # Execute tool calls (parallel if independent, serial if dependent)
            self._state.phase = AgentPhase.TOOL_EXECUTING
            if len(tool_calls) > 1 and self._can_parallelize(tool_calls, pre_parsed):
                # Clawd: one PreToolUse for the batch (not per-tool)
                get_clawd().tool_use(
                    tool_name=", ".join(tc["function"]["name"] for tc in tool_calls[:3]),
                    tool_input={"batch_size": len(tool_calls)},
                )

                results = await self._execute_parallel(tool_calls)
                batch_results = results
                self._state.tool_call_count += len(tool_calls)

                # Clawd: one PostToolUse for the batch
                all_ok = all(r.success for r in results)
                get_clawd().tool_result(tool_name="batch", success=all_ok)

                # One assistant message with ALL tool_calls (OpenAI standard)
                assistant_msg: dict[str, Any] = {
                    "role": "assistant", "content": text or None, "tool_calls": tool_calls,
                }
                if response.get("reasoning_content"):
                    assistant_msg["reasoning_content"] = response["reasoning_content"]
                self._append_message(assistant_msg)
                for tc, result in zip(tool_calls, results, strict=True):
                    self._warn_if_large_result(result, tc["function"]["name"])
                    self._store_tool_result(result, tc["id"], tool_name=tc["function"]["name"])
            else:
                # Clawd: one PreToolUse for the batch (not per-tool)
                get_clawd().tool_use(
                    tool_name=", ".join(tc["function"]["name"] for tc in tool_calls[:3]),
                    tool_input={"batch_size": len(tool_calls)},
                )

                for i, tc in enumerate(tool_calls):
                    self._state.tool_call_count += 1
                    tool_name = tc["function"]["name"]
                    arguments = pre_parsed.get(i, {})

                    result = await self._execute_tool(tool_name, arguments)
                    batch_results.append(result)
                    self._warn_if_large_result(result, tool_name)

                # One assistant message with ALL tool_calls (matching parallel path)
                assistant_msg: dict[str, Any] = {
                    "role": "assistant", "content": text or None, "tool_calls": tool_calls,
                }
                if response.get("reasoning_content"):
                    assistant_msg["reasoning_content"] = response["reasoning_content"]
                self._append_message(assistant_msg)
                for tc, result in zip(tool_calls, batch_results, strict=True):
                    self._store_tool_result(result, tc["id"], tool_name=tc["function"]["name"])

                # Clawd: one PostToolUse for the serial batch
                all_ok = all(r.success for r in batch_results)
                get_clawd().tool_result(tool_name="batch", success=all_ok)

            # ── Consecutive failure detection ──────────────────────────
            # When every tool call in a batch fails, increment counter.
            # Break the loop after N consecutive all-fail batches to
            # prevent infinite token burn when the model is stuck.
            if batch_results and not any(r.success for r in batch_results):
                _consecutive_failures += 1
                self._state.consecutive_failures = _consecutive_failures
                logger.warning("All %d tool(s) failed this turn (streak=%d/%d)",
                              len(batch_results), _consecutive_failures, _MAX_CONSECUTIVE_FAILURES)
                if _consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    self._state.phase = AgentPhase.ERROR
                    self._emit(ErrorEvent(
                        f"Too many consecutive tool failures "
                        f"({_consecutive_failures} batches). "
                        "The model may be stuck in a failure loop."
                    ))
                    # Clawd: error state — prevent stuck thinking animation
                    get_clawd().error(
                        "Too many consecutive tool failures"
                    )
                    break
            else:
                _consecutive_failures = 0  # any success resets the streak

        elapsed = time.time() - self._state.start_time
        self._emit(CompleteEvent(
            self._state.tool_call_count, elapsed,
            estimated_tokens=self.get_token_estimate(),
        ))

        # ── Auto-suggest memories ────────────────────────────────────────
        self._state.phase = AgentPhase.MEMORY_SUGGESTING
        if self.memory:
            try:
                user_msgs = [m.get("content", "") for m in self._state.messages
                            if m.get("role") == "user"]
                # Collect tool error messages so the memory system can learn
                # from failed patterns (e.g. "cd is blocked" → "use python subprocess")
                tool_errors = [m.get("content", "") for m in self._state.messages
                              if m.get("role") == "tool"
                              and m.get("content", "").startswith("Error:")]
                suggestions = self.memory.suggest_from_conversation(
                    user_msgs, tool_errors=tool_errors,
                )
                if suggestions:
                    logger.info("Memory suggestions: %d", len(suggestions))
                    # Store suggestions on the instance so the UI can display them
                    self._pending_memory_suggestions = suggestions
                    # Emit event so the REPL renders the suggestions
                    self._emit(MemorySuggestionEvent(suggestions=suggestions))
            except Exception:
                self._pending_memory_suggestions = []

        # Trigger extension point: on_run_complete
        self._ep_on_run_complete.trigger(
            task=task,
            result=final_response or "Task completed.",
            tool_call_count=self._state.tool_call_count,
        )

        return final_response or "Task completed."

    # ── Tool execution → agent_tools.py (ToolExecutionMixin)

    async def _streaming_chat(self, filtered_tools: list[dict] | None = None) -> Message:
        """Stream chat with tool collection."""
        collected_text = ""
        tool_calls: list[dict] = []
        reasoning_content = ""
        _thinking_sent = False  # throttle Clawd thinking updates

        async for delta_type, content in self.llm.chat_stream(
            self._state.messages,
            tools=filtered_tools or None,
            system_prompt=self._extract_system_prompt(),
        ):
            if delta_type == "text":
                collected_text += content
                self._emit(TextDeltaEvent(content))
                if not _thinking_sent:
                    get_clawd().thinking()
                    _thinking_sent = True
            elif delta_type == "tool_call":
                tool_calls.append(content)
            elif delta_type == "finish":
                pass
            elif delta_type == "reasoning":
                reasoning_content += content
                self._emit(ReasoningEvent(content))
                if not _thinking_sent:
                    get_clawd().thinking()
                    _thinking_sent = True

        result: Message = {
            "role": "assistant",
            "content": collected_text,
            "tool_calls": tool_calls,
        }
        if reasoning_content:
            result["reasoning_content"] = reasoning_content
        return result

    async def chat(self, message: str, stream: bool = True) -> str:
        """Continue conversation with follow-up.

        Mirrors the main run() loop: skill tool filtering, token compaction,
        consecutive-failure detection, and circuit breaker.
        """
        self._state.phase = AgentPhase.THINKING
        self._append_message({"role": "user", "content": message})

        SAFETY_LIMIT = 999  # circuit breaker
        _consecutive_failures = 0
        _MAX_CONSECUTIVE_FAILURES = 5

        while self._state.tool_call_count < SAFETY_LIMIT:
            # ── Token budget: auto-compact when approaching the limit (O(1)) ──
            if self._context_manager.should_compact():
                logger.warning("chat(): token budget %d/%d effective, auto-compacting",
                             self._context_manager.token_total,
                             self.config.agent.effective_context_tokens)
                await self.compact()
            if self._context_manager.needs_force_truncate():
                logger.critical("chat(): hard ceiling %d > 95%% of %d, force-truncating",
                               self._context_manager.token_total,
                               self.config.agent.max_context_tokens)
                self._force_truncate()

            # ── Skill tool filtering ────────────────────────────────────
            allowed_tool_names = self._compute_allowed_tools()
            filtered_tools = self._all_tools
            if allowed_tool_names is not None and len(allowed_tool_names) > 0:
                filtered_tools = [
                    t for t in self._all_tools
                    if t["function"]["name"] in allowed_tool_names
                    or t["function"]["name"].startswith("mcp__")
                ]

            if stream:
                response = await self._streaming_chat(filtered_tools)
            else:
                response = await self.llm.chat(
                    self._state.messages,
                    tools=filtered_tools,
                    system_prompt=self._extract_system_prompt(),
                )

            tool_calls = response.get("tool_calls", [])
            text = response.get("content", "")

            if not tool_calls:
                self._state.phase = AgentPhase.COMPLETED
                return text or "Done."

            # Execute tool calls (serial for safety in follow-up context)
            self._state.phase = AgentPhase.TOOL_EXECUTING
            batch_results: list[ToolResult] = []
            for tc in tool_calls:
                self._state.tool_call_count += 1
                tool_name = tc["function"]["name"]
                try:
                    arguments = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    arguments = {}

                result = await self._execute_tool(tool_name, arguments)
                batch_results.append(result)
                self._warn_if_large_result(result, tool_name)

            # Append ONE assistant message with ALL tool_calls (API protocol)
            assistant_msg: dict[str, Any] = {
                "role": "assistant", "content": text or None, "tool_calls": tool_calls,
            }
            if response.get("reasoning_content"):
                assistant_msg["reasoning_content"] = response["reasoning_content"]
            self._append_message(assistant_msg)
            for tc, result in zip(tool_calls, batch_results, strict=True):
                self._store_tool_result(result, tc["id"], tool_name=tc["function"]["name"])

            self._state.phase = AgentPhase.THINKING  # ready for next LLM turn

            # ── Consecutive failure detection ───────────────────────────
            if batch_results and not any(r.success for r in batch_results):
                _consecutive_failures += 1
                logger.warning("chat(): all %d tool(s) failed (streak=%d/%d)",
                             len(batch_results), _consecutive_failures, _MAX_CONSECUTIVE_FAILURES)
                if _consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    self._emit(ErrorEvent(
                        f"Too many consecutive tool failures "
                        f"({_consecutive_failures} batches)."
                    ))
                    break
            else:
                _consecutive_failures = 0

        self._state.phase = AgentPhase.COMPLETED
        return text or "Done."

    # ── Tool filtering → agent_tools.py (ToolExecutionMixin)

    # ── System prompt builder ─────────────────────────────────────────────

    def _build_system_prompt(self, user_input: str = "") -> str:
        """Build a context-rich system prompt from all subsystems.

        Delegates to the extracted SystemPromptBuilder so each section
        (environment, project, tools, MCP, memory, formatting) lives in
        its own method and can be tested individually.

        When *user_input* is provided, memory recall is targeted to the
        current task rather than returning a generic summary.
        """
        # Refresh model name on each build (may have changed via /model)
        self._prompt_builder.model = self.config.llm.model
        prompt = self._prompt_builder.build(TOOL_DEFINITIONS, user_input=user_input)
        # Trigger extension point: on_system_prompt_build
        self._ep_on_system_prompt.trigger(prompt=prompt, task=user_input)
        return prompt

    # ── Memory commands ───────────────────────────────────────────────────

    def remember(self, name: str, description: str, content: str,
                 memory_type: str = "reference") -> str:
        """Store a memory. Called by /remember command."""
        if not self.memory:
            return "Memory system not initialized."
        from .memory import Memory
        m = Memory(
            name=name,
            description=description,
            content=content,
            metadata={"type": memory_type},
        )
        self.memory.add(m)
        # Invalidate the cached system prompt so the next LLM call
        # picks up the new memory.
        self._cached_system_prompt = None
        return f"Memory saved: {name}"

    def recall(self, query: str) -> str:
        """Search memories. Called by /recall command."""
        if not self.memory:
            return "Memory system not initialized."
        results = self.memory.search(query)
        if not results:
            return f"No memories found for: {query}"
        lines = [f"Found {len(results)} memories:"]
        for m in results[:10]:
            lines.append(f"\n### {m.description}")
            lines.append(f"Type: {m.memory_type} | Updated: {m.updated}")
            lines.append(m.content[:300])
        return "\n".join(lines)

    # ── Helpers → agent_tools.py (ToolExecutionMixin)
    # ── Parallel execution → agent_tools.py (ToolExecutionMixin)
    # ── Undo / Redo / Dry-run ────────────────────────────────────────────

    def undo(self, count: int = 1) -> str:
        """Undo the last N changes."""
        if not self.change_tracker:
            return "Change tracker not available."
        reverted = self.change_tracker.undo(count)
        if not reverted:
            return "Nothing to undo."
        lines = [f"Undid {len(reverted)} change(s):"]
        for c in reverted:
            lines.append(f"  {c.summary}")
        return "\n".join(lines)

    def undo_all(self) -> str:
        """Undo all changes in this session."""
        if not self.change_tracker:
            return "Change tracker not available."
        reverted = self.change_tracker.undo_all()
        if not reverted:
            return "Nothing to undo."
        return f"Undid all {len(reverted)} changes."

    def restore_change(self, change_id: int) -> str:
        """Re-apply a reverted change."""
        if not self.change_tracker:
            return "Change tracker not available."
        restored = self.change_tracker.restore(change_id)
        if restored:
            return f"Restored: {restored.summary}"
        return f"Change #{change_id} not found or not reverted."

    def list_changes(self) -> str:
        """List all changes in this session."""
        if not self.change_tracker:
            return "Change tracker not available."
        return self.change_tracker.summary()

    def show_change_diff(self, last_n: int = 3) -> str:
        """Show diffs for recent changes."""
        if not self.change_tracker:
            return "Change tracker not available."
        return self.change_tracker.diff_summary(last_n)

    def toggle_dry_run(self, enabled: bool | None = None) -> str:
        """Enable or disable dry-run mode."""
        if not self.change_tracker:
            return "Change tracker not available."
        if enabled is None:
            enabled = not self.change_tracker.dry_run
        self.change_tracker.dry_run = enabled
        if enabled:
            return "DRY-RUN MODE ON — changes will be PREVIEWED only, not applied."
        return "DRY-RUN MODE OFF — changes will be applied normally."

    def _extract_system_prompt(self) -> str:
        """Return the system prompt from the current conversation state.

        Cached — only re-scans when messages[0] is replaced (e.g. after compaction).
        """
        if self._cached_system_prompt is not None:
            return self._cached_system_prompt
        for m in self._state.messages:
            if m.get("role") == "system":
                self._cached_system_prompt = m.get("content", "")
                return self._cached_system_prompt
        return ""

    # ── Utility ───────────────────────────────────────────────────────────

    # save_session / _auto_save_session / _do_save / session_id
    # → AgentSessionMixin (agent_session.py)

    # Compaction → agent_compact.py (CompactionMixin)

    # ── Change tracking helper → agent_tools.py (ToolExecutionMixin._read_old_content)

    def _append_message(self, msg: Message) -> None:
        """Append a message to state AND context manager (O(1) token update)."""
        self._state.messages.append(msg)
        self._context_manager.append(msg)

    def get_token_estimate(self) -> int:
        """O(1) token total from ContextManager. Falls back to LLM count if stale."""
        if self._context_manager.messages:
            return self._context_manager.token_total
        return self.llm.count_tokens_approx(self._state.messages)

    # get_conversation_summary / reset → AgentSessionMixin (agent_session.py)

    async def shutdown(self) -> None:
        """Clean up resources."""
        # Clawd: final SessionEnd
        get_clawd().shutdown()
        await self.llm.close()
        if self._summary_llm:
            await self._summary_llm.close()
        if self.mcp:
            await self.mcp.stop_all()
        self.tools.close()
