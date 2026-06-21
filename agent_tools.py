"""Tool execution, parallel dispatch, filtering, and result storage — mixin for CoderAgent."""
import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from .tools import ToolResult
from .core import ToolCallEvent, ToolResultEvent, ToolStreamEvent, ErrorEvent

logger = logging.getLogger(__name__)


class ToolExecutionMixin:
    """Tool execution with fool-proof checks, parallel dispatch, and filtering.

    Contract (host class: ``CoderAgent``):
        Requires:
        - ``self.fool_proof`` — FoolProof instance
        - ``self.permissions`` — PermissionManager instance
        - ``self.mcp`` — MCPClient | None
        - ``self.config`` — AppConfig instance
        - ``self._tool_executor`` — ToolExecutor instance
        - ``self._stream_cb`` — Callable | None (streaming callback)
        - ``self._self_correct_depth`` / ``self._max_self_correct_depth`` — int guards
        Provides:
        - ``_execute_tool()`` — dispatch to builtin or MCP, with self-correction
        - ``_execute_parallel()`` — asyncio.gather for independent tool calls
        - ``_filter_tools_for_skill()`` — restrict tool set by active skill
    """

    # ── Tool execution ────────────────────────────────────────────────────

    # Guard depth for self-correction retry — prevents infinite recursion.
    # These are set as INSTANCE variables in CoderAgent.__init__ to avoid
    # cross-session contamination under ThreadingHTTPServer (server mode).
    _MAX_SELF_CORRECT_DEPTH: int = 1

    async def _execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        """Execute a tool with fool-proof checks, dispatching to built-in or MCP.

        Wraps synchronous tool execution in asyncio.to_thread() as a bridge
        until tools.py is fully async (Phase 1.7).
        """
        source = "mcp" if (self.mcp and self.mcp.is_mcp_tool(tool_name)) else "builtin"

        # ── Fool-proof evaluation ──────────────────────────────────────
        if self.fool_proof:
            from .fool_proof import ActionRequired
            check = self.fool_proof.evaluate(tool_name, arguments)

            if check.action == ActionRequired.BLOCKED:
                msg = f"BLOCKED: {check.confirm_message}"
                self._emit(ErrorEvent(msg))
                return ToolResult(success=False, output="", error=msg)

            # CONFIRM / WARN_CONFIRM: ask user interactively.
            # Only DANGER-level operations reach here (CRITICAL is BLOCKED above,
            # CAUTION/SAFE are PROCEED). The fool_proof engine already checked
            # category allow/deny rules — these actions mean the user must decide.
            if check.action in (ActionRequired.CONFIRM, ActionRequired.WARN_CONFIRM):
                if self.permissions:
                    allowed = self.permissions.check(tool_name, arguments)
                    if not allowed:
                        msg = f"User denied permission for {tool_name}"
                        self._emit(ErrorEvent(msg))
                        return ToolResult(success=False, output="", error=msg)

        # ── Permission check (fallback when fool_proof is disabled) ─────
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
                if self.privilege_mgr.needs_elevation(command):
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
                mcp_result = await self.mcp.call_tool(tool_name, arguments)
                output = self._format_mcp_result(mcp_result)
                result = ToolResult(success=True, output=output)
                self._emit(ToolResultEvent(tool_name, result, source="mcp", arguments=arguments))
                return result
            except Exception as e:
                result = ToolResult(success=False, output="", error=str(e))
                self._emit(ToolResultEvent(tool_name, result, source="mcp", arguments=arguments))
                return result

        # Capture old content before write/edit (for change tracker + diff)
        file_path = arguments.get("file_path", "")
        old_file_content = self._read_old_content(file_path) if tool_name in ("write_file", "edit_file") and file_path else ""

        # Execute built-in tool with self-correction
        if self.change_tracker and self.change_tracker.dry_run and tool_name in ("write_file", "edit_file"):
            # Dry-run: skip actual file write, only track in change_tracker via capture below
            result = ToolResult(success=True, output=f"[DRY-RUN] Would {tool_name}: {arguments.get('file_path', '')}")
        else:
            # Set up real-time streaming for long-running tools
            if tool_name in ("run_shell", "web_search", "web_fetch"):
                def _on_stream(tool_name: str, chunk: str):
                    self._emit(ToolStreamEvent(tool_name, chunk))
                self.tools.set_stream_callback(_on_stream)
            try:
                result = await self.tools.execute(tool_name, arguments)
            finally:
                if tool_name in ("run_shell", "web_search", "web_fetch"):
                    self.tools.set_stream_callback(None)

        # Record successful file changes in the change tracker.
        # Skip in dry-run mode: no actual files were modified, so there is
        # nothing to undo — recording a phantom change would corrupt state.
        if (result.success and self.fool_proof
                and tool_name in ("write_file", "edit_file")
                and not (self.change_tracker and self.change_tracker.dry_run)):
            self.fool_proof.capture(tool_name, arguments, result, old_content=old_file_content)

        # Self-correction: if failed, try to diagnose and fix
        if (not result.success and self.self_correct and self.self_correct.should_retry(tool_name, arguments)
                and self._self_correct_depth < self._MAX_SELF_CORRECT_DEPTH):
            # Preserve the original command so suggest_fix uses it as the base
            # for pip install prepending (prevents exponential command growth).
            if tool_name == "run_shell" and "_original_command" not in arguments:
                arguments = dict(arguments, _original_command=arguments.get("command", ""))
            diagnosis = self.self_correct.diagnose(result.error, tool_name, arguments)
            if diagnosis and diagnosis.retry_strategy == "auto_fix":
                fixed_args = self.self_correct.suggest_fix(tool_name, arguments, diagnosis, error_message=result.error)
                if fixed_args and fixed_args != arguments:
                    logger.info("Auto-correcting: %s (was: %s)", diagnosis.fix_suggestion[:80], result.error[:80])
                    # Retry with fixed args THROUGH the full safety pipeline
                    self._self_correct_depth += 1
                    try:
                        corrected_result = await self._execute_tool(tool_name, fixed_args)
                    finally:
                        self._self_correct_depth -= 1
                    self.self_correct.record_attempt(
                        tool_name, arguments, result.error,
                        diagnosis, fixed_args,
                        corrected_result.success,
                    )
                    if corrected_result.success:
                        self._emit(ToolResultEvent(tool_name, corrected_result, source="builtin"))
                        return corrected_result

        self._emit(ToolResultEvent(tool_name, result, source="builtin", arguments=arguments))
        return result

    # ── Parallel execution ──────────────────────────────────────────────

    @staticmethod
    def _can_parallelize(tool_calls: list[dict], pre_parsed: dict[int, dict] | None = None) -> bool:
        """Check if tool calls can run in parallel (no shared write targets).

        *pre_parsed* maps index → pre-parsed arguments dict, avoiding
        redundant JSON parsing when the caller has already decoded them.
        """
        write_targets = set()
        for i, tc in enumerate(tool_calls):
            name = tc["function"]["name"]
            if name == "run_shell":
                return False  # Shell commands have side effects, serialize
            if name.startswith("mcp__"):
                return False  # MCP tools may have arbitrary side effects
            if name in ("write_file", "edit_file"):
                if pre_parsed and i in pre_parsed:
                    fp = pre_parsed[i].get("file_path", "")
                else:
                    try:
                        fp = json.loads(tc["function"]["arguments"]).get("file_path", "")
                    except json.JSONDecodeError:
                        return False
                if fp in write_targets:
                    return False
                write_targets.add(fp)
        return True

    async def _execute_parallel(self, tool_calls: list[dict]) -> list[ToolResult]:
        """Execute multiple tool calls concurrently via asyncio.gather."""
        async def run_one(idx: int, tc: dict) -> tuple[int, ToolResult]:
            tool_name = tc["function"]["name"]
            try:
                arguments = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                arguments = {}
            result = await self._execute_tool(tool_name, arguments)
            return idx, result

        tasks = [run_one(i, tc) for i, tc in enumerate(tool_calls)]
        gathered = await asyncio.gather(*tasks, return_exceptions=True)

        results: list[ToolResult] = []
        for item in gathered:
            if isinstance(item, BaseException):
                # CancelledError inherits from BaseException, not Exception.
                # Re-raise it so the cancellation signal propagates correctly.
                if isinstance(item, asyncio.CancelledError):
                    raise item
                logger.error("Parallel tool execution failed: %s", item)
                results.append(ToolResult(success=False, output="", error=str(item)))
            else:
                results.append(item[1])  # (idx, result) tuple
        return results

    # ── Tool filtering (multi-skill) ────────────────────────────────────

    def _compute_allowed_tools(self) -> set[str] | None:
        """Compute effective tool restrictions from all active skill extensions.

        Cached per run — invalidated on skill change (new call to run()).

        Rule: intersection of non-empty tool restrictions across all active
        skill extensions. Empty list = no restriction (all tools allowed).
        Returns None if no restrictions exist.
        """
        if self._cached_allowed_tools is not None:
            return self._cached_allowed_tools
        restrictions: list[set[str]] = []
        for ext in self.ext_mgr.list_active():
            if "skill" not in ext.meta.tags:
                continue
            tools = ext.get_tools()
            if tools:  # non-empty = this extension restricts tools
                restrictions.append(set(tools))

        if not restrictions:
            self._cached_allowed_tools = None
            return None  # no restrictions → all tools allowed

        # Intersection of all non-empty restrictions
        allowed = restrictions[0]
        for r in restrictions[1:]:
            allowed &= r
        result = allowed if allowed else None
        self._cached_allowed_tools = result
        return result

    # ── Result formatting & storage ─────────────────────────────────────

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

    def _store_tool_result(self, result: ToolResult, tool_call_id: str,
                           tool_name: str = "") -> None:
        """Truncate tool output and append to message history.

        Full output is available during execution, but only a capped version
        is stored for future LLM turns to prevent context bloat.
        """
        # Trigger extension point: on_tool_result
        if tool_name:
            self._ep_on_tool_result.trigger(tool_name=tool_name, result=result)
        cap = self.config.agent.max_message_output_chars
        content = result.to_message()
        if len(content) > cap:
            content = (
                content[:cap]
                + f"\n\n... [truncated {len(content) - cap:,} chars "
                + f"from {result.output.count(chr(10)) + 1} lines]"
            )
        tool_msg = {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
        }
        self._state.messages.append(tool_msg)
        self._context_manager.append(tool_msg)  # keep CM token tracking in sync

    @staticmethod
    def _warn_if_large_result(result: ToolResult, tool_name: str) -> None:
        """Log a warning when a tool result is unusually large."""
        size = len(result.output)
        if size > 30_000:
            logger.warning(
                "Large tool result: %s → %d chars (~%d tokens)",
                tool_name, size, size // 4,
            )

    # ── Change tracking helpers ─────────────────────────────────────────

    def _read_old_content(self, file_path: str) -> str:
        """Read the current content of a file before editing (for change tracking).

        Uses the tool executor's file cache when possible to avoid a redundant
        disk read if the file was recently read by _tool_read_file.
        """
        if not file_path:
            return ""
        p = Path(file_path)
        if not p.is_absolute():
            p = self.tools.workspace / p
        if not p.exists():
            return ""

        # Check file cache first (populated by _tool_read_file)
        # Cache format: (mtime, cached_at, content) — 3-tuple with LRU timestamp
        cache_key = str(p.resolve())
        try:
            if cache_key in self.tools._file_cache:
                cached_mtime, _, cached_content = self.tools._file_cache[cache_key]
                if cached_mtime == p.stat().st_mtime:
                    return cached_content
        except (ValueError, KeyError):
            pass  # cache format changed — fall through to disk read

        try:
            # Safety: skip files > 50MB to avoid OOM
            if p.stat().st_size > 50_000_000:
                logger.warning("Skipping large file for change tracking: %s", p)
                return f"[file too large: {p.stat().st_size / 1_000_000:.0f}MB]"
            return p.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return ""
        except Exception:
            logger.debug("Failed to read %s for change tracking", file_path, exc_info=True)
        return ""
