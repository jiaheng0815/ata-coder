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

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import copy

from .config import AgentConfig, AppConfig
from .llm_client import LLMClient, SYSTEM_PROMPT, Message
from .anthropic_client import AnthropicClient
from .tools import ToolExecutor, TOOL_DEFINITIONS, ToolResult
from .agent_subsystems import AgentSubsystems
from .system_prompt_builder import SystemPromptBuilder
from .fool_proof import FoolProofEngine
from .change_tracker import ChangeTracker
from .privilege import PrivilegeManager
from .task_planner import TaskPlanner
from .self_correct import SelfCorrectionEngine
from .git_workflow import GitWorkflow
from .extension import ExtensionManager, get_extension_manager
from .skill_extension import SkillExtension
from .model_router import get_subagent_model
from .settings import get_settings

logger = logging.getLogger(__name__)


# ── Event types ──────────────────────────────────────────────────────────────

@dataclass
class AgentEvent:
    """Base event."""
    pass


@dataclass
class TextDeltaEvent(AgentEvent):
    text: str


@dataclass
class ToolCallEvent(AgentEvent):
    tool_name: str
    arguments: dict[str, Any]
    source: str = "builtin"  # "builtin" or "mcp"


@dataclass
class ToolResultEvent(AgentEvent):
    tool_name: str
    result: ToolResult
    source: str = "builtin"
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ThinkingEvent(AgentEvent):
    pass


@dataclass
class ReasoningEvent(AgentEvent):
    """The model is thinking/reasoning (DeepSeek R1/v4 thinking mode)."""
    text: str


@dataclass
class SkillChangedEvent(AgentEvent):
    skill_name: str


@dataclass
class ErrorEvent(AgentEvent):
    error: str


@dataclass
class CompleteEvent(AgentEvent):
    total_tool_calls: int
    total_time: float


# ── Agent state ──────────────────────────────────────────────────────────────

@dataclass
class AgentState:
    messages: list[Message] = field(default_factory=list)
    tool_call_count: int = 0
    start_time: float = 0.0


# ── The Agent ────────────────────────────────────────────────────────────────

class CoderAgent:
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

        # Choose client: Anthropic or OpenAI format
        if os.environ.get("ATA_CODER_USE_ANTHROPIC") == "1":
            self.llm = AnthropicClient(self.config.llm)
            self._use_anthropic = True
        else:
            self.llm = LLMClient(self.config.llm)
            self._use_anthropic = False

        self.tools = tool_executor or ToolExecutor(self.config.agent)

        # ── Subsystems ────────────────────────────────────────────────────
        self.subsys = subsystems or AgentSubsystems()
        self.skills = self.subsys.skills
        self.memory = self.subsys.memory
        self.mcp = self.subsys.mcp
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
        self.planner = TaskPlanner()
        self.self_correct = SelfCorrectionEngine(max_retries=3)
        self.git = GitWorkflow(self.tools.workspace)

        self._state = AgentState()
        self._on_event: Callable[[AgentEvent], None] | None = None
        self._current_session_id: str = ""
        self._pending_memory_suggestions: list[str] = []

        # Build the combined tool list
        self._all_tools = list(TOOL_DEFINITIONS)
        if self.mcp:
            mcp_tools = self.mcp.get_tools()
            self._all_tools.extend(mcp_tools)
            logger.info(
                "MCP tools added: %d", len(mcp_tools),
            )

        # Extension tools
        ext_tools = self.ext_mgr.aggregate_tools()
        if ext_tools:
            self._all_tools.extend(ext_tools)
            logger.info("Extension tools added: %d", len(ext_tools))

        logger.info(
            "Total tools: %d builtin + %s MCP + %s extensions = %d",
            len(TOOL_DEFINITIONS),
            len(self.mcp.get_tools()) if self.mcp else 0,
            len(ext_tools),
            len(self._all_tools),
        )

        self.llm.register_tools(self._all_tools)

        # ── Sub-agent manager (set later by AgentController if used) ──────
        self._sub_agent_mgr = None

    # ── Model routing ──────────────────────────────────────────────────────

    def _route_model(self, model: str) -> None:
        """Switch the LLM client to use a different model at runtime."""
        if self.llm.config.model == model:
            return
        logger.info("Switching model: %s → %s", self.llm.config.model, model)
        self.llm.config.model = model

    @property
    def current_model(self) -> str:
        return self.llm.config.model

    def _ai_classify(self, task: str) -> str:
        """
        Use the cheap model to classify task complexity.
        Returns 'simple', 'complex', or 'normal'.

        Uses a SEPARATE client instance for classification — never mutates
        self.llm, so this is safe for concurrent server-mode requests.

        Shortcut: very short tasks → simple (skip API call)
                  very long tasks → complex (skip API call)
        """
        settings = get_settings()

        # ── Length shortcut ─────────────────────────────────────────────
        shortcut = settings.shortcut_classify(task)
        if shortcut is not None:
            logger.info("AI classify (shortcut): %.60s → %s", task, shortcut)
            return shortcut

        # ── AI classification via independent cheap-model client ────────
        cheap_model = get_subagent_model()

        # Copy config and override model — creates an independent client
        from .config import LLMConfig
        classify_config = copy.deepcopy(self.llm.config)
        classify_config.model = cheap_model

        # Build a fresh client for classification only
        if self._use_anthropic:
            from .anthropic_client import AnthropicClient
            classify_client = AnthropicClient(classify_config)
        else:
            from .llm_client import LLMClient
            classify_client = LLMClient(classify_config)

        classify_prompt = (
            "You are a task complexity classifier. "
            "Analyze the following user request and respond with EXACTLY one word: "
            "'simple' or 'complex'.\n\n"
            "SIMPLE = quick question, explanation, lookup, small fix, single-file edit.\n"
            "COMPLEX = implementing features, refactoring, debugging, architecture, "
            "multi-file changes, deep analysis.\n\n"
            f"User request: {task}\n\n"
            "Complexity (simple/complex):"
        )

        try:
            response = classify_client.chat(
                [{"role": "user", "content": classify_prompt}],
                tools=[],
            )
            answer = (response.get("content") or "").strip().lower()
            result = "complex" if "complex" in answer else "simple"
        except Exception as e:
            logger.warning("AI classification failed, defaulting to normal: %s", e)
            result = "normal"
        finally:
            classify_client.close()

        logger.info("AI classify: %.60s → %s (via %s)", task, result, cheap_model)
        return result

    # ── Extension management ────────────────────────────────────────────────

    def _register_skills_as_extensions(self) -> None:
        """Register all loaded SkillManager skills as SkillExtension adapters."""
        if not self.subsys.has_skills:
            return
        for skill in self.subsys.skills.list_skills():
            ext = SkillExtension(skill)
            if self.ext_mgr.register(ext):
                logger.debug("Registered skill extension: skill:%s", skill.name)
            else:
                logger.debug("Skill extension already registered: skill:%s", skill.name)

    def _discover_extensions(self) -> None:
        """Discover extensions from configured extension directories."""
        ext_dirs = getattr(self.config.agent, "extension_dirs", [])
        if not ext_dirs:
            return
        for d in ext_dirs:
            loaded = self.ext_mgr.discover(d)
            if loaded:
                logger.info("Discovered %d extensions in %s", len(loaded), d)

    def _register_extension_points(self) -> None:
        """Register hook points extensions can tap into."""
        self._ep_on_run_start = self.ext_mgr.extension_point(
            "on_agent_run_start",
            "Called when agent.run() starts — (task, skill_name)"
        )
        self._ep_on_run_complete = self.ext_mgr.extension_point(
            "on_agent_run_complete",
            "Called when agent.run() completes — (task, result, tool_call_count)"
        )
        self._ep_on_tool_execute = self.ext_mgr.extension_point(
            "on_tool_execute",
            "Called before each tool execution — (tool_name, arguments)"
        )
        self._ep_on_tool_result = self.ext_mgr.extension_point(
            "on_tool_result",
            "Called after each tool result — (tool_name, result)"
        )
        self._ep_on_system_prompt = self.ext_mgr.extension_point(
            "on_system_prompt_build",
            "Called during system prompt construction — (prompt, task)"
        )
        self._ep_on_model_route = self.ext_mgr.extension_point(
            "on_model_route",
            "Called after model routing — (task, complexity, model)"
        )
        logger.debug(
            "Registered %d extension points",
            len(self.ext_mgr.list_extension_points()),
        )

    def set_sub_agent_manager(self, mgr: Any) -> None:
        """Set the SubAgentManager for spawn_subagent tool support."""
        self._sub_agent_mgr = mgr

    # ── Event system ──────────────────────────────────────────────────────

    def on_event(self, callback: Callable[[AgentEvent], None]) -> None:
        self._on_event = callback

    def _emit(self, event: AgentEvent) -> None:
        """Emit event to both callback and EventQueue (if available)."""
        # Support EventQueue (thread-safe agent→UI communication)
        event_queue = getattr(self, "_event_queue", None)
        if event_queue is not None:
            event_queue.put(event)
        # Backward-compatible callback
        if self._on_event:
            self._on_event(event)

    # ── Main entry point ──────────────────────────────────────────────────

    def run(self, task: str, stream: bool = True, skill_name: str | None = None,
            explicit_model: str = "") -> str:
        """
        Run the agent on a given task.

        Args:
            task: User task description
            stream: Enable streaming output
            skill_name: Force a specific skill (or None for auto-detect)
            explicit_model: Explicit model override (bypasses auto-routing)

        Returns:
            Final response text
        """
        self._state = AgentState(start_time=time.time())

        # ── Model routing ──────────────────────────────────────────────────
        if explicit_model:
            # User specified a model — use it directly, no classification needed
            self._route_model(explicit_model)
            self._routed_complexity = "explicit"
        else:
            # AI-driven routing: cheap model classifies → route accordingly
            settings = get_settings()

            complexity = self._ai_classify(task)

            if complexity == "simple":
                routed_model = settings.model_haiku
            elif complexity == "complex":
                routed_model = settings.model_opus
            elif complexity == "normal":
                routed_model = settings.default_model
            else:
                logger.warning("Unknown complexity %r, using default model", complexity)
                routed_model = settings.default_model

            # Adjust by effort level
            effort = getattr(self.config, "effort", "medium")
            if effort == "low":
                routed_model = settings.model_haiku
            elif effort == "max":
                routed_model = settings.model_opus
            # high/medium use the complexity-based route as-is

            self._route_model(routed_model)
            self._routed_complexity = complexity

        logger.info("Model: %s (complexity=%s)", self.current_model, self._routed_complexity)

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

        # Skill selection (multi-skill support)
        if skill_name and self.skills:
            skill = self.skills.activate(skill_name, merge=True)
            if skill:
                self._emit(SkillChangedEvent(skill.name))
        elif self.skills:
            detected = self.skills.detect_skills(task, max_results=3)
            for skill in detected:
                if skill.name != "general-coder":
                    self.skills.activate(skill.name, merge=True)
                    self._emit(SkillChangedEvent(skill.name))

        # Build system prompt — pass the user task for targeted memory recall
        system_prompt = self._build_system_prompt(task)

        self._state.messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ]

        logger.info("Agent run: skill=%s, model=%s, session=%s, task=%.100s",
                     self.skills.active_skill.name if self.skills and self.skills.active_skill else "default",
                     routed_model,
                     self._current_session_id,
                     task)

        final_response = ""
        last_text = ""
        SAFETY_LIMIT = 999  # circuit breaker — not a tool-call "limit"

        while True:
            # Circuit breaker: prevent infinite loop when the model keeps
            # emitting tool calls (hallucination / API bug).  This is NOT a
            # user-facing tool limit — just a last-resort safety net.
            if self._state.tool_call_count >= SAFETY_LIMIT:
                logger.critical(
                    "SAFETY_LIMIT reached: %d tool calls. Breaking loop.",
                    self._state.tool_call_count,
                )
                self._emit(ErrorEvent(
                    f"Safety limit reached ({SAFETY_LIMIT} tool calls). "
                    "The model may be stuck in a tool-call loop."
                ))
                break

            self._emit(ThinkingEvent())

            # Auto-compact when approaching the effective context limit.
            # effective_context_tokens (default 200k) reflects the range where
            # the model actually pays attention, not the theoretical 1M window.
            # We compact at 80% of effective limit, which is well below the
            # theoretical max_context_tokens.
            est_tokens = self.get_token_estimate()
            max_tokens = self.config.agent.max_context_tokens
            effective = self.config.agent.effective_context_tokens
            if est_tokens > effective:
                logger.warning("Token budget: %d/%d effective (%.0f%% of %d max), auto-compacting",
                             est_tokens, effective, est_tokens / max_tokens * 100, max_tokens)
                self.compact()

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
                response = self._streaming_chat(filtered_tools)
            else:
                chat_kw: dict = {"tools": filtered_tools}
                if self._use_anthropic:
                    sys_msg = ""
                    for m in self._state.messages:
                        if m.get("role") == "system":
                            sys_msg = m.get("content", "")
                            break
                    chat_kw["system_prompt"] = sys_msg
                response = self.llm.chat(self._state.messages, **chat_kw)

            tool_calls = response.get("tool_calls", [])
            text = response.get("content", "")

            if text:
                last_text = text

            if not tool_calls:
                final_response = text or last_text
                break

            # Execute tool calls (parallel if independent, serial if dependent)
            if len(tool_calls) > 1 and self._can_parallelize(tool_calls):
                results = self._execute_parallel(tool_calls, text)
                # One assistant message with ALL tool_calls (OpenAI standard)
                assistant_msg: dict[str, Any] = {
                    "role": "assistant", "content": text or None, "tool_calls": tool_calls,
                }
                if response.get("reasoning_content"):
                    assistant_msg["reasoning_content"] = response["reasoning_content"]
                self._state.messages.append(assistant_msg)
                for tc, result in zip(tool_calls, results):
                    self._warn_if_large_result(result, tc["function"]["name"])
                    self._store_tool_result(result, tc["id"])
            else:
                for tc in tool_calls:
                    self._state.tool_call_count += 1
                    tool_name = tc["function"]["name"]
                    try:
                        arguments = json.loads(tc["function"]["arguments"])
                    except json.JSONDecodeError as e:
                        logger.error("Tool args parse error: %s", e)
                        self._state.messages.append({
                            "role": "assistant", "content": text or None, "tool_calls": [tc],
                        })
                        self._state.messages.append({
                            "role": "tool", "tool_call_id": tc["id"],
                            "content": f"Error: invalid JSON arguments: {e}"
                        })
                        continue

                    result = self._execute_tool(tool_name, arguments)
                    self._warn_if_large_result(result, tool_name)

                    assistant_msg: dict[str, Any] = {
                        "role": "assistant", "content": text or None, "tool_calls": [tc],
                    }
                    if response.get("reasoning_content"):
                        assistant_msg["reasoning_content"] = response["reasoning_content"]

                    self._state.messages.append(assistant_msg)
                    self._store_tool_result(result, tc["id"])

        elapsed = time.time() - self._state.start_time
        self._emit(CompleteEvent(self._state.tool_call_count, elapsed))

        # ── Auto-suggest memories ────────────────────────────────────────
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
            except Exception:
                self._pending_memory_suggestions = []

        # Deactivate skill after task
        if self.skills:
            self.skills.deactivate()

        # Trigger extension point: on_run_complete
        self._ep_on_run_complete.trigger(
            task=task,
            result=final_response or "Task completed.",
            tool_call_count=self._state.tool_call_count,
        )

        return final_response or "Task completed."

    def _execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        """Execute a tool with fool-proof checks, dispatching to built-in or MCP."""
        source = "mcp" if (self.mcp and self.mcp.is_mcp_tool(tool_name)) else "builtin"

        # ── Fool-proof evaluation ──────────────────────────────────────
        if self.fool_proof:
            from .fool_proof import ActionRequired
            check = self.fool_proof.evaluate(tool_name, arguments)

            if check.action == ActionRequired.BLOCKED:
                msg = f"BLOCKED: {check.confirm_message}"
                self._emit(ErrorEvent(msg))
                return ToolResult(success=False, output="", error=msg)

            if check.action in (ActionRequired.CONFIRM, ActionRequired.WARN_CONFIRM):
                # Already handled by permission prompt flow
                if self.permissions:
                    from .permissions import tool_category
                    category = tool_category(tool_name)
                    if category != "read":
                        allowed = self.permissions.check(tool_name, arguments)
                        if not allowed:
                            return ToolResult(
                                success=False, output="",
                                error=f"User denied permission for {tool_name}"
                            )

        # ── Permission check (fallback) ────────────────────────────────
        elif self.permissions:
            from .permissions import tool_category
            category = tool_category(tool_name)
            if category != "read":
                allowed = self.permissions.check(tool_name, arguments)
                if not allowed:
                    msg = f"User denied permission for {tool_name}"
                    self._emit(ErrorEvent(msg))
                    return ToolResult(success=False, output="", error=msg)

        # ── Privilege check (needs elevation?) ────────────────────────
        if tool_name == "run_shell" and self.privilege_mgr:
            command = arguments.get("command", "")
            allowed, reason = self.privilege_mgr.check_dangerous_command(command)
            if not allowed:
                self._emit(ErrorEvent(reason))
                return ToolResult(success=False, output="", error=reason)

            # Audit privileged operations
            if self.privilege_mgr.is_dangerous:
                self.privilege_mgr.audit_operation(tool_name, arguments)
                # Wrap command with elevation if needed
                if self.privilege_mgr._needs_elevation(command):
                    arguments = dict(arguments)
                    original = arguments["command"]
                    arguments["command"] = self.privilege_mgr.wrap_command(original, force_elevation=True)
                    logger.info("Elevated command: %s", arguments["command"][:100])

        # Trigger extension point: on_tool_execute
        self._ep_on_tool_execute.trigger(tool_name=tool_name, arguments=arguments)

        self._emit(ToolCallEvent(tool_name, arguments, source=source))

        # Execute MCP tool
        if source == "mcp":
            try:
                mcp_result = self.mcp.call_tool(tool_name, arguments)
                output = self._format_mcp_result(mcp_result)
                result = ToolResult(success=True, output=output)
                self._emit(ToolResultEvent(tool_name, result, source="mcp", arguments=arguments))
                return result
            except Exception as e:
                result = ToolResult(success=False, output="", error=str(e))
                self._emit(ToolResultEvent(tool_name, result, source="mcp", arguments=arguments))
                return result

        # Capture old content before write/edit (for change tracker + diff)
        if tool_name == "edit_file":
            old_file_content = self._read_old_content(arguments.get("file_path", ""))
        else:
            old_file_content = ""

        # Execute built-in tool with self-correction
        if self.change_tracker and self.change_tracker.dry_run and tool_name in ("write_file", "edit_file"):
            # Dry-run: skip actual file write, only track in change_tracker via capture below
            result = ToolResult(success=True, output=f"[DRY-RUN] Would {tool_name}: {arguments.get('file_path', '')}")
        else:
            result = self.tools.execute(tool_name, arguments)

        # Record successful file changes in the change tracker
        if result.success and self.fool_proof and tool_name in ("write_file", "edit_file"):
            self.fool_proof.capture(tool_name, arguments, result, old_content=old_file_content)

        # Self-correction: if failed, try to diagnose and fix
        if not result.success and self.self_correct and self.self_correct.should_retry(tool_name, arguments):
            diagnosis = self.self_correct.diagnose(result.error, tool_name, arguments)
            if diagnosis and diagnosis.retry_strategy == "auto_fix":
                fixed_args = self.self_correct.suggest_fix(tool_name, arguments, diagnosis)
                if fixed_args and fixed_args != arguments:
                    self._emit(ToolResultEvent(tool_name, result, source="builtin", arguments=arguments))
                    logger.info("Auto-correcting: %s (was: %s)", diagnosis.fix_suggestion[:80], result.error[:80])
                    # Retry with fixed args
                    corrected_result = self.tools.execute(tool_name, fixed_args)
                    self.self_correct.record_attempt(
                        tool_name, arguments, result.error,
                        diagnosis, fixed_args,
                        corrected_result.success,
                    )
                    if corrected_result.success:
                        self._emit(ToolResultEvent(tool_name, corrected_result, source="builtin"))
                        # Record in change tracker after auto-fixed edit
                        if self.fool_proof and tool_name in ("write_file", "edit_file"):
                            self.fool_proof.capture(tool_name, fixed_args, corrected_result,
                                                    old_content=old_file_content if tool_name == "edit_file" else "")
                        return corrected_result

        self._emit(ToolResultEvent(tool_name, result, source="builtin", arguments=arguments))
        return result

    def _format_mcp_result(self, mcp_result: Any) -> str:
        """Format MCP tool result into a string."""
        if isinstance(mcp_result, dict):
            content = mcp_result.get("content", [])
            if isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            parts.append(item.get("text", ""))
                        elif item.get("type") == "resource":
                            parts.append(json.dumps(item.get("resource", {})))
                        else:
                            parts.append(json.dumps(item))
                    else:
                        parts.append(str(item))
                return "\n".join(parts)
            return json.dumps(mcp_result)
        return str(mcp_result)

    def _streaming_chat(self, filtered_tools: list[dict] | None = None) -> Message:
        """Stream chat with tool collection."""
        collected_text = ""
        tool_calls: list[dict] = []
        reasoning_content = ""

        # Anthropic client takes system prompt separately
        stream_args = [self._state.messages]
        stream_kw: dict = {"tools": filtered_tools or None}
        if self._use_anthropic:
            # Extract system prompt from messages
            sys_msg = ""
            for m in self._state.messages:
                if m.get("role") == "system":
                    sys_msg = m.get("content", "")
                    break
            stream_kw["system_prompt"] = sys_msg

        for delta_type, content in self.llm.chat_stream(*stream_args, **stream_kw):
            if delta_type == "text":
                collected_text += content
                self._emit(TextDeltaEvent(content))
            elif delta_type == "tool_call":
                tool_calls.append(content)
            elif delta_type == "finish":
                pass
            elif delta_type == "reasoning":
                reasoning_content += content
                self._emit(ReasoningEvent(content))

        result: Message = {
            "role": "assistant",
            "content": collected_text,
            "tool_calls": tool_calls,
        }
        if reasoning_content:
            result["reasoning_content"] = reasoning_content
        return result

    def chat(self, message: str, stream: bool = True) -> str:
        """Continue conversation with follow-up."""
        self._state.messages.append({"role": "user", "content": message})

        if stream:
            response = self._streaming_chat()
        else:
            response = self.llm.chat(self._state.messages)

        tool_calls = response.get("tool_calls", [])
        text = response.get("content", "")
        SAFETY_LIMIT = 999  # circuit breaker

        while (tool_calls
               and self._state.tool_call_count < SAFETY_LIMIT
               and self._state.tool_call_count < self.config.agent.max_tool_calls):
            for tc in tool_calls:
                self._state.tool_call_count += 1
                tool_name = tc["function"]["name"]
                try:
                    arguments = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    arguments = {}

                result = self._execute_tool(tool_name, arguments)
                self._state.messages.append({
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": [tc],
                })
                self._store_tool_result(result, tc["id"])

            if stream:
                response = self._streaming_chat()
            else:
                response = self.llm.chat(self._state.messages)

            tool_calls = response.get("tool_calls", [])
            new_text = response.get("content", "")
            if new_text:
                text = new_text

        return text or "Done."

    # ── Tool filtering (multi-skill) ──────────────────────────────────────

    def _compute_allowed_tools(self) -> set[str] | None:
        """Compute effective tool restrictions from all active skill extensions.

        Rule: intersection of non-empty tool restrictions across all active
        skill extensions. Empty list = no restriction (all tools allowed).
        Returns None if no restrictions exist.
        """
        restrictions: list[set[str]] = []
        for ext in self.ext_mgr.list_active():
            if "skill" not in ext.meta.tags:
                continue
            tools = ext.get_tools()
            if tools:  # non-empty = this extension restricts tools
                restrictions.append(set(tools))

        if not restrictions:
            return None  # no restrictions → all tools allowed

        # Intersection of all non-empty restrictions
        allowed = restrictions[0]
        for r in restrictions[1:]:
            allowed &= r
        return allowed if allowed else None

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
        return self._prompt_builder.build(TOOL_DEFINITIONS, user_input=user_input)

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

    # ── Helpers ──────────────────────────────────────────────────────

    def _store_tool_result(self, result: ToolResult, tool_call_id: str) -> None:
        """Truncate tool output and append to message history.

        Full output is available during execution, but only a capped version
        is stored for future LLM turns to prevent context bloat.
        """
        cap = self.config.agent.max_message_output_chars
        content = result.to_message()
        if len(content) > cap:
            content = (
                content[:cap]
                + f"\n\n... [truncated {len(content) - cap:,} chars "
                + f"from {result.output.count(chr(10)) + 1} lines]"
            )
        self._state.messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
        })

    @staticmethod
    def _warn_if_large_result(result: ToolResult, tool_name: str) -> None:
        """Log a warning when a tool result is unusually large."""
        size = len(result.output)
        if size > 30_000:
            logger.warning(
                "Large tool result: %s → %d chars (~%d tokens)",
                tool_name, size, size // 4,
            )

    # ── Parallel execution ──────────────────────────────────────────

    def _can_parallelize(self, tool_calls: list[dict]) -> bool:
        """Check if tool calls can run in parallel (no shared write targets)."""
        write_targets = set()
        for tc in tool_calls:
            name = tc["function"]["name"]
            if name in ("write_file", "edit_file"):
                try:
                    args = json.loads(tc["function"]["arguments"])
                    fp = args.get("file_path", "")
                except json.JSONDecodeError:
                    return False
                if fp in write_targets:
                    return False  # two tools writing to same file
                write_targets.add(fp)
            elif name == "run_shell":
                # Shell commands might have side effects, serialize
                return False
        return True

    def _execute_parallel(self, tool_calls: list[dict], text: str = "") -> list[ToolResult]:
        """Execute multiple tool calls concurrently."""
        import concurrent.futures
        results: list[ToolResult | None] = [None] * len(tool_calls)

        def run_one(idx: int, tc: dict) -> tuple[int, ToolResult]:
            tool_name = tc["function"]["name"]
            try:
                arguments = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                arguments = {}
            result = self._execute_tool(tool_name, arguments)
            return idx, result

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(tool_calls), 4)) as pool:
            futures = {pool.submit(run_one, i, tc): i for i, tc in enumerate(tool_calls)}
            for future in concurrent.futures.as_completed(futures):
                idx, result = future.result()
                results[idx] = result

        return [r for r in results if r is not None]

    # ── Undo / Changes / Dry-run ───────────────────────────────────────

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
        else:
            return "DRY-RUN MODE OFF — changes will be applied normally."

    # ── Utility ───────────────────────────────────────────────────────────

    def save_session(self, session_id: str = "") -> str:
        """Save current conversation to session storage."""
        if not self.sessions:
            return "Session storage not available."
        sid = session_id or self._current_session_id
        if not sid:
            from .session import generate_session_id
            sid = generate_session_id("manual-save")
        first_user_msg = ""
        for msg in self._state.messages:
            if msg.get("role") == "user":
                first_user_msg = msg.get("content", "")[:200]
                break
        self.sessions.save(
            session_id=sid,
            messages=self._state.messages,
            summary=first_user_msg,
            skill=self.skills.active_skill.name if self.skills and self.skills.active_skill else "",
            model=self.config.llm.model,
            workspace=str(self.tools.workspace),
            tool_call_count=self._state.tool_call_count,
        )
        self._current_session_id = sid
        return sid


    # ── Compaction token budget ──────────────────────────────────────────
    RECENT_TOKEN_BUDGET = 80_000   # max tokens to keep from recent messages
    COMPACT_IF_FEWER_THAN = 6      # skip compaction if fewer than this many msgs

    def compact(self) -> str:
        """
        Compact conversation by summarising old messages.

        Strategy: keep system prompt + recent messages up to
        RECENT_TOKEN_BUDGET tokens, summarise everything in between using
        a cheap LLM call.  Falls back to a lightweight extractive summary
        if the API call fails.

        Uses token budget for recent messages (not a fixed count) so that
        large file reads don't survive compaction intact.
        """
        if len(self._state.messages) <= self.COMPACT_IF_FEWER_THAN:
            return "Already compact."

        system_msg = self._state.messages[0]
        all_but_system = self._state.messages[1:]

        # Walk backwards through recent messages, accumulating up to the budget
        recent: list[Message] = []
        recent_tokens = 0
        for msg in reversed(all_but_system):
            msg_tokens = self._estimate_message_tokens(msg)
            if recent_tokens + msg_tokens > self.RECENT_TOKEN_BUDGET and recent:
                # Stop — we've filled the recent budget
                break
            recent.insert(0, msg)
            recent_tokens += msg_tokens

        # The middle is everything NOT in recent and NOT the system msg
        kept_count = len(recent)
        middle = all_but_system[:-kept_count] if kept_count > 0 else all_but_system

        if not middle:
            return "Already compact (all messages fit in recent budget)."

        # Extract key facts from middle messages for the fallback summary
        tool_count = sum(1 for m in middle if m.get("tool_calls"))
        user_msgs = [m.get("content", "")[:200] for m in middle if m.get("role") == "user"]
        file_ops = self._collect_file_ops(middle)

        summary = self._summarise_messages(middle, file_ops, user_msgs, tool_count)

        truncated: list[Message] = [
            system_msg,
            {"role": "user", "content": "[Conversation summary]\\n" + summary},
            {"role": "assistant", "content": "Understood. I'll continue with the remaining context using the summary above."},
        ]
        truncated.extend(recent)
        old_count = len(self._state.messages)
        old_tokens = self.get_token_estimate()
        self._state.messages = truncated
        new_tokens = self.get_token_estimate()

        logger.info("Compacted: %d→%d msgs, ~%d→%d tokens (files: %d, tools: %d, recent_budget: %d)",
                    old_count, len(truncated), old_tokens, new_tokens,
                    len(file_ops), tool_count, recent_tokens)
        return (f"Compacted from {old_count}→{len(truncated)} messages "
                f"(~{old_tokens:,}→~{new_tokens:,} tokens, {len(file_ops)} files, {tool_count} tool calls).")

    def _estimate_message_tokens(self, msg: Message) -> int:
        """Rough token estimate for a single message."""
        content = msg.get("content", "") or ""
        # Use the LLM client's estimator if available
        try:
            return self.llm.count_tokens_approx([msg])
        except Exception:
            # CJK-aware fallback
            import re
            cjk = len(re.findall(r'[一-鿿　-〿＀-￯]', content))
            other = len(content) - cjk
            tokens = (cjk * 2 // 3) + (other // 4)
            for tc in msg.get("tool_calls", []):
                try:
                    tokens += len(json.dumps(tc)) // 4
                except Exception:
                    pass
            return max(1, tokens)

    def _collect_file_ops(self, messages: list[Message]) -> list[str]:
        """Collect files modified in a message list."""
        ops: list[str] = []
        for m in messages:
            for tc in m.get("tool_calls", []):
                fn = tc.get("function", {})
                if fn.get("name") in ("write_file", "edit_file"):
                    try:
                        args = json.loads(fn.get("arguments", "{}"))
                        fp = args.get("file_path", "")
                        if fp:
                            ops.append(fp)
                    except json.JSONDecodeError:
                        pass
        return ops

    def _summarise_messages(self, middle: list[Message], file_ops: list[str],
                            user_msgs: list[str], tool_count: int) -> str:
        """Generate a summary of the middle conversation segment.

        Attempts a cheap LLM call first; falls back to a lightweight extractive
        summary so the user never loses context entirely.
        """
        # ── LLM-based summary (best effort) ──────────────────────────────
        try:
            summary_prompt = (
                "Summarise this conversation segment in 3-5 bullet points. "
                "Focus on: what the user asked, what files were changed, what "
                "decisions were made, and any unresolved issues. "
                "Be concise — this summary will replace the full conversation "
                "history to save context tokens.\n\n"
                f"Files modified: {', '.join(file_ops) if file_ops else 'none'}\n"
                f"Tool calls: {tool_count}\n"
                f"User requests: {'; '.join(user_msgs[:5])}\n"
            )
            # Use a separate cheap-model client for summarisation
            from .config import LLMConfig
            summary_config = copy.deepcopy(self.llm.config)
            summary_config.model = get_subagent_model()
            if self._use_anthropic:
                from .anthropic_client import AnthropicClient
                sc = AnthropicClient(summary_config)
            else:
                from .llm_client import LLMClient
                sc = LLMClient(summary_config)
            try:
                resp = sc.chat([{"role": "user", "content": summary_prompt}], tools=[])
                llm_summary = (resp.get("content") or "").strip()
                if llm_summary:
                    # Include extractive data as context for the LLM summary
                    parts = [llm_summary]
                    if file_ops:
                        parts.append(f"\nFiles touched: {', '.join(file_ops[:10])}")
                    return "\n".join(parts)
            finally:
                sc.close()
        except Exception:
            logger.debug("LLM summarisation unavailable, using extractive fallback")

        # ── Extractive fallback ─────────────────────────────────────────
        parts = [f"Summarised {len(middle)} messages ({tool_count} tool calls)."]
        if user_msgs:
            parts.append(f"Topics: {'; '.join(user_msgs[:5])}")
        if file_ops:
            parts.append(f"Files modified: {', '.join(file_ops[:10])}")
        return "\n".join(parts)

    @property
    def session_id(self) -> str:
        return self._current_session_id

    def _read_old_content(self, file_path: str) -> str:
        """Read the current content of a file before editing (for change tracking)."""
        if not file_path:
            return ""
        p = Path(file_path)
        if not p.is_absolute():
            p = self.tools.workspace / p
        try:
            if p.exists():
                # Safety: skip files > 50MB to avoid OOM
                if p.stat().st_size > 50_000_000:
                    logger.warning("Skipping large file for change tracking: %s", p)
                    return f"[file too large: {p.stat().st_size / 1_000_000:.0f}MB]"
                return p.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return ""
        except Exception:
            logger.debug(
                "Failed to read %s for change tracking", file_path, exc_info=True
            )
        return ""

    def get_token_estimate(self) -> int:
        """Estimate total tokens in the conversation."""
        return self.llm.count_tokens_approx(self._state.messages)

    def get_conversation_summary(self) -> str:
        msgs = self._state.messages
        total = len(msgs)
        tool_calls = sum(1 for m in msgs if m.get("tool_calls"))
        user_msgs = sum(1 for m in msgs if m.get("role") == "user")
        tokens = self.get_token_estimate()
        return (
            f"Session: {self._current_session_id or 'unsaved'}\n"
            f"Messages: {total} ({user_msgs} user turns, {tool_calls} tool calls)\n"
            f"Tokens: ~{tokens:,} / {self.config.agent.max_context_tokens:,}\n"
            f"Skill: {self.skills.active_skill.name if self.skills and self.skills.active_skill else 'default'}\n"
            f"Model: {self.config.llm.model}"
        )

    def reset(self) -> None:
        self._state = AgentState(start_time=time.time())
        self._current_session_id = ""
        if self.skills:
            self.skills.deactivate()
        logger.info("Agent state reset")

    def shutdown(self) -> None:
        """Clean up resources."""
        self.llm.close()
        if self.mcp:
            self.mcp.stop_all()
