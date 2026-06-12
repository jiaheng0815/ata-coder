"""
Tool system for the ATA Coder.

Provides a set of tools the agent can use:
- read_file: Read file contents
- write_file: Create or overwrite a file
- edit_file: Precise string replacement in a file
- run_shell: Execute a shell command
- grep: Search file contents with regex
- glob: Find files matching a pattern
- list_dir: List directory contents
- web_search: Search the web (optional)
"""

import html
import html.parser
import logging
import os
import re
import subprocess
import fnmatch
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

import httpx

from .config import AgentConfig

logger = logging.getLogger(__name__)


# ── Tool result type ─────────────────────────────────────────────────────────

class ToolResult:
    """Result of executing a tool."""

    def __init__(self, success: bool, output: str, error: str = ""):
        self.success = success
        self.output = output
        self.error = error

    def to_message(self) -> str:
        """Format as a message to the LLM."""
        if self.success:
            return self.output
        return f"Error: {self.error}\n\n{self.output}".strip()

    def to_tool_result(self, tool_call_id: str) -> dict:
        """Format as an OpenAI tool result message."""
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": self.to_message(),
        }


# ── Tool definitions (OpenAI function format) ────────────────────────────────

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file. Returns the file content with line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "The absolute or relative path to the file to read.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Line number to start reading from (1-based).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of lines to read.",
                    },
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file. Creates the file if it doesn't exist, overwrites if it does.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "The absolute or relative path to the file to write.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The content to write to the file.",
                    },
                },
                "required": ["file_path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Perform exact string replacement in a file. The old_string must match exactly (including whitespace/indentation) and be unique in the file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "The absolute or relative path to the file to edit.",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "The exact text to find and replace.",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "The text to replace it with.",
                    },
                },
                "required": ["file_path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Execute a shell command and return stdout/stderr. Use for build, test, lint, git, and other development commands.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default: 120).",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents using regular expressions. Returns matching files and lines.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "The regex pattern to search for.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory or file to search in. Defaults to current directory.",
                    },
                    "glob": {
                        "type": "string",
                        "description": "Filter files by glob pattern (e.g. '*.py', 'src/**/*.ts').",
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "description": "Whether search is case-sensitive (default: false).",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files matching a glob pattern. Returns sorted list of matching file paths.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern to match (e.g. '**/*.py', 'src/**/*.ts').",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory to search in. Defaults to current directory.",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List contents of a directory with file types and sizes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path to list. Defaults to current directory.",
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "Whether to list recursively (default: false).",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web using DuckDuckGo (free, no API key). Returns titles, URLs, and snippets for the given query. Use when you need up-to-date information beyond your knowledge cutoff.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results (default: 10, max: 20).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch a web page and extract its text content. Strips HTML/scripts/CSS, returns plain text. Use after web_search to read the full content of a result. Caps at 15,000 characters.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to fetch (must be a full http/https URL).",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_subagent",
            "description": "Spawn a sub-agent to work on a task in parallel. The sub-agent runs in a background thread with its own isolated context window (no access to the main conversation history). Use for parallel searches, independent analysis, or delegating self-contained work. Returns immediately with the agent ID — use collect_subagent to get results. Max 5 concurrent sub-agents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "The task to delegate. Must be self-contained — the sub-agent has NO context from the main conversation. Be specific about what to do and what format to return results in.",
                    },
                    "skill": {
                        "type": "string",
                        "description": "Optional skill name for the sub-agent (e.g., 'code-reviewer', 'debugger', 'test-writer').",
                    },
                    "model": {
                        "type": "string",
                        "description": "Optional model override for the sub-agent. Use a cheaper/faster model for simple tasks.",
                    },
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "collect_subagent",
            "description": "Collect results from a previously spawned sub-agent. Blocks until the sub-agent completes or times out. The sub-agent's full message history is available in the result for context injection.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "The agent ID returned by spawn_subagent.",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Max wait time in seconds (default: 300).",
                    },
                },
                "required": ["agent_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_subagents",
            "description": "List all sub-agents and their statuses (running, done, failed, cancelled). Use to check on spawned sub-agents.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
]


# ── Tool implementations ─────────────────────────────────────────────────────

class ToolExecutor:
    """Executes tool calls and manages workspace context."""

    # Result limits
    MAX_GREP_RESULTS = 100
    MAX_GREP_PER_FILE = 20
    MAX_GLOB_RESULTS = 200
    MAX_DIR_ENTRIES = 500

    # Directories to skip during recursive operations
    SKIP_DIRS = {
        "node_modules", "__pycache__", ".git", "venv", ".venv",
        "dist", "build", "target", ".next", ".pytest_cache",
    }

    def __init__(self, config: AgentConfig | None = None):
        self.config = config or AgentConfig()
        self.workspace = Path(self.config.workspace_dir).resolve()
        self._edit_callback: Callable[[str, str], None] | None = None
        # File read cache: path -> (mtime, content).  "只读一遍" — files
        # are read from disk once per session and served from memory after.
        self._file_cache: dict[str, tuple[float, str]] = {}
        self._cache_dir: Path | None = None
        # Sub-agent manager (set by agent)
        self._sub_agent_mgr: Any = None

    def set_sub_agent_manager(self, mgr: Any) -> None:
        """Set the SubAgentManager for spawn/collect sub-agent tool support."""
        self._sub_agent_mgr = mgr

    def setup_file_cache(self, cache_dir: str | Path) -> None:
        """Create session cache directory. Call once before running the agent."""
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def clear_file_cache(self) -> None:
        """Remove all cached files and the cache directory."""
        self._file_cache.clear()
        if self._cache_dir and self._cache_dir.exists():
            import shutil
            shutil.rmtree(self._cache_dir, ignore_errors=True)
            self._cache_dir = None

    def close(self) -> None:
        """Release all held resources (httpx clients, file caches)."""
        self.clear_file_cache()
        if hasattr(self, "_http") and self._http is not None:
            try:
                self._http.close()
            except Exception:
                pass
            self._http = None

    def __del__(self) -> None:
        """Safety net: ensure httpx client is closed on GC."""
        try:
            self.close()
        except Exception:
            pass

    def on_edit(self, callback: Callable[[str, str], None]) -> None:
        """Register callback for edit notifications: callback(file_path, old_content)."""
        self._edit_callback = callback

    def _notify_edit(self, file_path: str, old_content: str) -> None:
        """Notify the UI of a file edit for diff display."""
        if self._edit_callback:
            try:
                self._edit_callback(file_path, old_content)
            except Exception:
                logger.exception("Edit callback failed for %s", file_path)

    def _resolve_path(self, file_path: str) -> Path:
        """Resolve a file path relative to workspace."""
        p = Path(file_path)
        if p.is_absolute():
            return p
        return self.workspace / p

    def _ensure_in_workspace(self, path: Path) -> Path:
        """
        Ensure path is within workspace for safety.
        For files outside workspace, allow with a warning.
        """
        try:
            path.resolve().relative_to(self.workspace)
        except ValueError:
            logger.warning("Path outside workspace: %s", path)
        return path

    # Global output cap — every tool result is trimmed to this many chars.
    # ~25k tokens, still generous for legitimate work but prevents a single
    # result from eating the whole context window.
    MAX_OUTPUT_CHARS = 100_000

    def execute(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        """Dispatch a tool call to the appropriate handler.

        All results are capped at MAX_OUTPUT_CHARS to prevent any single
        tool response from blowing up the conversation context.
        """
        handler = getattr(self, f"_tool_{tool_name}", None)
        if handler is None:
            return ToolResult(
                success=False,
                output="",
                error=f"Unknown tool: {tool_name}",
            )
        try:
            result = handler(**arguments)
        except Exception as e:
            logger.exception("Tool %s failed", tool_name)
            return ToolResult(
                success=False, output="", error=f"{type(e).__name__}: {e}"
            )

        # ── Global size cap ──────────────────────────────────────────
        if len(result.output) > self.MAX_OUTPUT_CHARS:
            original_len = len(result.output)
            cut = result.output[:self.MAX_OUTPUT_CHARS]
            result.output = (
                cut + f"\n\n... [truncated {original_len - self.MAX_OUTPUT_CHARS:,} "
                f"chars — result was {original_len:,} chars total]"
            )
            logger.warning(
                "Tool %s output truncated: %d → %d chars",
                tool_name, original_len, self.MAX_OUTPUT_CHARS,
            )

        return result

    # ── File tools ───────────────────────────────────────────────────────────

    # ── Output limits (prevent token bloat from large file reads) ──────
    MAX_READ_LINES = 2000        # auto-truncate reads without an explicit limit
    MAX_READ_CHARS = 80_000      # hard cap on output chars (~20k tokens)

    def _tool_read_file(
        self,
        file_path: str,
        offset: int | None = None,
        limit: int | None = None,
    ) -> ToolResult:
        """Read a file with optional line range.

        When *limit* is not set, output is capped at MAX_READ_LINES to
        prevent a single file read from eating tens of thousands of context
        tokens.  Use *offset* + *limit* to page through larger files.
        """
        path = self._resolve_path(file_path)
        if not path.exists():
            return ToolResult(
                success=False,
                output="",
                error=f"File not found: {path}",
            )
        if path.is_dir():
            return ToolResult(
                success=False,
                output="",
                error=f"Path is a directory, not a file: {path}",
            )

        # ── File cache: "只读一遍" — return short note on re-read ────
        cache_key = str(path.resolve())
        current_mtime = path.stat().st_mtime
        needs_disk_read = True

        if cache_key in self._file_cache:
            cached_mtime, cached_content = self._file_cache[cache_key]
            if cached_mtime == current_mtime:
                if offset is not None or limit is not None:
                    # Specific section — serve from memory, skip disk
                    needs_disk_read = False
                    lines = cached_content.splitlines(keepends=True)
                    if lines and not lines[-1].endswith("\n"):
                        lines[-1] += "\n"
                else:
                    # Whole file re-read — DON'T send content again
                    total = cached_content.count("\n") + 1
                    chars = len(cached_content)
                    return ToolResult(
                        success=True,
                        output=(
                            f"[cached] {file_path} — {total} lines, {chars:,} chars.\n"
                            f"Already in conversation context from earlier read. "
                            f"Use offset/limit to read specific sections if needed."
                        ),
                    )

        if needs_disk_read:
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    raw = f.read()
            except Exception as e:
                return ToolResult(
                    success=False, output="", error=f"Cannot read file: {e}"
                )
            self._file_cache[cache_key] = (current_mtime, raw)
            lines = raw.splitlines(keepends=True)
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            # Mirror to disk cache dir if configured
            if self._cache_dir:
                # Cross-platform safe filename: strip drive letter on Windows,
                # replace path separators with underscores
                resolved = str(path.resolve())
                if len(resolved) >= 2 and resolved[1] == ":":
                    resolved = resolved[2:]  # strip "C:"
                safe_name = resolved.lstrip("\\/").replace("\\", "_").replace("/", "_")
                try:
                    (self._cache_dir / safe_name).write_text(raw, encoding="utf-8")
                except Exception:
                    pass

        total_lines = len(lines)
        user_specified_range = limit is not None

        # Default cap: when the caller didn't ask for a specific range,
        # truncate to MAX_READ_LINES so one file doesn't blow the context.
        effective_limit = limit if user_specified_range else min(
            len(lines), self.MAX_READ_LINES
        )

        start = (offset or 1) - 1
        end = start + effective_limit

        # Clamp
        start = max(0, start)
        end = min(len(lines), end)

        selected = lines[start:end]
        was_truncated = (end - start) < (total_lines - start) if not user_specified_range else False
        # Also truncate if the user explicitly asked for a range but total
        # output still exceeds the hard char cap (safety net).
        char_truncated = False

        # Format with line numbers
        output_lines: list[str] = []
        chars = 0
        for i, line in enumerate(selected, start=start + 1):
            formatted = f"{i:6d}\t{line.rstrip()}"
            chars += len(formatted) + 1  # +1 for newline
            if chars > self.MAX_READ_CHARS:
                output_lines.append(f"... (output truncated at {self.MAX_READ_CHARS} chars, use offset/limit to read more)")
                char_truncated = True
                break
            output_lines.append(formatted)

        truncated = was_truncated or char_truncated
        shown_lines = len(output_lines) - (1 if char_truncated else 0)
        header = f"File: {path} (lines {start+1}-{start+shown_lines} of {total_lines}"
        if truncated:
            header += f", truncated — use offset/limit to page"
        header += ")\n"
        return ToolResult(success=True, output=header + "\n".join(output_lines))

    def _tool_write_file(
        self, file_path: str, content: str
    ) -> ToolResult:
        """Create or overwrite a file."""
        path = self._resolve_path(file_path)
        self._ensure_in_workspace(path)

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            size = path.stat().st_size
            return ToolResult(
                success=True,
                output=f"File written: {path} ({size} bytes, {content.count(chr(10))} lines)",
            )
        except Exception as e:
            return ToolResult(
                success=False, output="", error=f"Cannot write file: {e}"
            )

    def _tool_edit_file(
        self, file_path: str, old_string: str, new_string: str
    ) -> ToolResult:
        """Replace text in a file with exact string matching. Captures old content for diff."""
        if old_string == new_string:
            return ToolResult(
                success=False,
                output="",
                error="old_string and new_string are identical",
            )

        path = self._resolve_path(file_path)
        if not path.exists():
            return ToolResult(
                success=False,
                output="",
                error=f"File not found: {path}",
            )

        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            return ToolResult(
                success=False, output="", error=f"Cannot read file: {e}"
            )

        count = content.count(old_string)
        if count == 0:
            return ToolResult(
                success=False,
                output="",
                error="old_string not found in file. Check whitespace/indentation.",
            )
        if count > 1:
            return ToolResult(
                success=False,
                output="",
                error=f"old_string found {count} times in file. Must be unique. Use a larger string with more surrounding context.",
            )

        # Store old content for diff display (via UI callback)
        old_content = content

        new_content = content.replace(old_string, new_string, 1)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_content)

            # Notify UI of the edit for diff display
            self._notify_edit(str(path), old_content)

            return ToolResult(
                success=True,
                output=f"File edited: {path} (1 replacement)",
            )
        except Exception as e:
            return ToolResult(
                success=False, output="", error=f"Cannot write file: {e}"
            )

    # ── Shell tool ───────────────────────────────────────────────────────────

    def _tool_run_shell(
        self, command: str, timeout: int = 120
    ) -> ToolResult:
        """Execute a shell command."""
        # Safety checks
        cmd_lower = command.lower().strip()

        # Check blocked patterns
        for blocked in self.config.blocked_commands:
            if blocked.lower() in cmd_lower:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Blocked command pattern detected: {blocked}",
                )

        # Check if first word is allowed
        first_word = command.strip().split()[0] if command.strip() else ""
        if first_word and first_word not in self.config.allowed_commands:
            return ToolResult(
                success=False,
                output="",
                error=(
                    f"Command '{first_word}' is not in the allowed list. "
                    f"Allowed commands: {', '.join(sorted(self.config.allowed_commands[:10]))}..."
                ),
            )

        try:
            result = subprocess.run(
                command,
                shell=True,  # shell=True required for pipeline/redirects; SafetyGuard blocks dangerous patterns
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(self.workspace),
            )
            output = result.stdout
            if result.stderr:
                output += f"\n[stderr]\n{result.stderr}"
            if result.returncode != 0:
                output += f"\n[exit code: {result.returncode}]"
            return ToolResult(
                success=result.returncode == 0,
                output=output.strip() or "(no output)",
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                output="",
                error=f"Command timed out after {timeout}s",
            )
        except Exception as e:
            return ToolResult(
                success=False, output="", error=f"Command failed: {e}"
            )

    # ── Search tools ─────────────────────────────────────────────────────────

    def _tool_grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        case_sensitive: bool = False,
    ) -> ToolResult:
        """Search file contents with regex."""
        search_dir = self._resolve_path(path or ".")
        if not search_dir.exists():
            return ToolResult(
                success=False,
                output="",
                error=f"Directory not found: {search_dir}",
            )

        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            return ToolResult(
                success=False, output="", error=f"Invalid regex: {e}"
            )

        results: list[str] = []
        total_matches = 0

        for root, dirs, files in os.walk(search_dir):
            dirs[:] = [
                d for d in dirs
                if not d.startswith(".")
                and d not in self.SKIP_DIRS
            ]
            for fname in files:
                if glob and not fnmatch.fnmatch(fname, glob):
                    continue

                full_path = os.path.join(root, fname)
                try:
                    rel_path = os.path.relpath(full_path, self.workspace)
                except ValueError:
                    rel_path = full_path

                try:
                    with open(full_path, "r", encoding="utf-8", errors="replace") as fh:
                        file_lines = fh.readlines()
                except Exception:
                    continue

                matches_in_file = []
                for line_no, line_text in enumerate(file_lines, 1):
                    if regex.search(line_text):
                        matches_in_file.append(
                            f"  {line_no}: {line_text.rstrip()[:200]}"
                        )
                        total_matches += 1
                        if len(matches_in_file) >= self.MAX_GREP_PER_FILE:
                            matches_in_file.append("  ... (truncated)")
                            break

                if matches_in_file:
                    results.append(f"{rel_path}:")
                    results.extend(matches_in_file)

                if len(results) >= self.MAX_GREP_RESULTS:
                    results.append("... (result limit reached)")
                    break

            if len(results) >= self.MAX_GREP_RESULTS:
                break

        if not results:
            return ToolResult(
                success=True,
                output=f"No matches found for pattern: {pattern}",
            )
        return ToolResult(
            success=True,
            output=f"Found {total_matches} matches:\n\n" + "\n".join(results),
        )

    def _tool_glob(
        self,
        pattern: str,
        path: str | None = None,
    ) -> ToolResult:
        """Find files by glob pattern."""
        search_dir = self._resolve_path(path or ".")
        if not search_dir.exists():
            return ToolResult(
                success=False,
                output="",
                error=f"Directory not found: {search_dir}",
            )

        import glob as glob_mod

        # Use recursive glob
        search_pattern = str(search_dir / pattern)
        matches = glob_mod.glob(search_pattern, recursive=True)

        # Also try non-recursive if recursive produced nothing
        if not matches and "**" not in pattern:
            search_pattern = str(search_dir / "**" / pattern)
            matches = glob_mod.glob(search_pattern, recursive=True)

        if not matches:
            return ToolResult(
                success=True, output=f"No files matching: {pattern}"
            )

        # Sort and format
        matches.sort()
        output_lines = []
        for m in matches[:200]:
            try:
                rel = os.path.relpath(m, self.workspace)
            except ValueError:
                rel = m
            size = os.path.getsize(m) if os.path.isfile(m) else 0
            output_lines.append(f"  {rel}  ({size:,} bytes)")

        if len(matches) > 200:
            output_lines.append(
                f"  ... and {len(matches) - 200} more files"
            )

        return ToolResult(
            success=True,
            output=f"Found {len(matches)} files matching '{pattern}':\n"
            + "\n".join(output_lines),
        )

    def _tool_list_dir(
        self,
        path: str | None = None,
        recursive: bool = False,
    ) -> ToolResult:
        """List directory contents."""
        target = self._resolve_path(path or ".")
        if not target.exists():
            return ToolResult(
                success=False,
                output="",
                error=f"Directory not found: {target}",
            )
        if not target.is_dir():
            return ToolResult(
                success=False,
                output="",
                error=f"Not a directory: {target}",
            )

        output_lines = [f"Directory: {target}"]
        entries: list[str] = []

        if recursive:
            for root, dirs, files in os.walk(target):
                dirs[:] = [
                    d for d in dirs
                    if not d.startswith(".") and d not in self.SKIP_DIRS
                ]
                level = root.replace(str(target), "").count(os.sep)
                indent = "  " * level
                if level > 0:
                    entries.append(f"{indent}{os.path.basename(root)}/")
                for f in sorted(files):
                    fp = os.path.join(root, f)
                    size = os.path.getsize(fp)
                    entries.append(f"{indent}  {f}  ({size:,}B)")
        else:
            items = sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name))
            for item in items:
                suffix = "/" if item.is_dir() else ""
                size = ""
                if item.is_file():
                    size = f"  ({item.stat().st_size:,}B)"
                entries.append(f"  {item.name}{suffix}{size}")

        output_lines.extend(entries[:500])
        if len(entries) > 500:
            output_lines.append(f"  ... and {len(entries) - 500} more entries")

        return ToolResult(success=True, output="\n".join(output_lines))


    # ── Web tools ──────────────────────────────────────────────────────────

    # Internal HTTP client (lazy-init, shared across web tools)
    _http: httpx.Client | None = None

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(
                timeout=httpx.Timeout(30.0),
                follow_redirects=True,
                headers={
                    "User-Agent": (
                        "ATA-Coder/2.0 (AI Coding Assistant; "
                        "+https://github.com/ata-coder/ata-coder)"
                    ),
                    "Accept": "text/html,application/xhtml+xml,*/*",
                    "Accept-Language": "en-US,zh-CN;q=0.9",
                },
            )
        return self._http

    def _tool_web_search(
        self,
        query: str,
        max_results: int = 10,
    ) -> ToolResult:
        """Search the web via DuckDuckGo Lite (no API key required)."""
        max_results = min(max(max_results, 1), 20)

        try:
            url = f"https://lite.duckduckgo.com/lite/?"
            resp = self.http.post(
                url, data={"q": query, "kl": "us-en"},
            )
            resp.raise_for_status()
        except httpx.TimeoutException:
            return ToolResult(
                success=False, output="",
                error="Search timed out. Try again or check your network."
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                return ToolResult(
                    success=False, output="",
                    error="DuckDuckGo rate limited. Wait a few seconds and retry."
                )
            return ToolResult(
                success=False, output="",
                error=f"Search failed: HTTP {e.response.status_code}"
            )
        except Exception as e:
            return ToolResult(
                success=False, output="",
                error=f"Search failed: {e}"
            )

        # Parse DuckDuckGo Lite HTML
        results = self._parse_ddg_lite(resp.text)
        if not results:
            return ToolResult(
                success=True,
                output=f"No results found for: {query}"
            )

        # Format
        out = [f"Search results for: {query}\n"]
        for i, r in enumerate(results[:max_results], 1):
            out.append(f"{i}. **{html.unescape(r['title'])}**")
            out.append(f"   {r['url']}")
            out.append(f"   {html.unescape(r['snippet'])}")
            out.append("")

        return ToolResult(success=True, output="\n".join(out))

    @staticmethod
    def _parse_ddg_lite(html_text: str) -> list[dict[str, str]]:
        """Extract search results from DuckDuckGo Lite HTML."""
        results: list[dict[str, str]] = []

        # DDG Lite: results are in <a> tags with class="result-link"
        # and snippets in <td class="result-snippet">
        link_pattern = re.compile(
            r'<a[^>]*href="([^"]*)"[^>]*class="[^"]*result-link[^"]*"[^>]*>(.*?)</a>',
            re.DOTALL | re.IGNORECASE,
        )
        snippet_pattern = re.compile(
            r'<td[^>]*class="[^"]*result-snippet[^"]*"[^>]*>(.*?)</td>',
            re.DOTALL | re.IGNORECASE,
        )

        links = link_pattern.findall(html_text)
        snippets = snippet_pattern.findall(html_text)

        for i, (href, title) in enumerate(links):
            href = html.unescape(href.strip())
            title = re.sub(r'<[^>]+>', '', title).strip()
            if not title:
                continue

            # Pick corresponding snippet
            snippet = ""
            if i < len(snippets):
                snippet = re.sub(r'<[^>]+>', '', snippets[i])
                snippet = html.unescape(snippet.strip())

            results.append({
                "title": title,
                "url": href,
                "snippet": snippet[:300],
            })

        return results

    def _tool_web_fetch(self, url: str) -> ToolResult:
        """Fetch a URL and extract its text content."""
        if not url.startswith(("http://", "https://")):
            return ToolResult(
                success=False, output="",
                error=f"Invalid URL: must start with http:// or https://"
            )

        try:
            resp = self.http.get(url)
            resp.raise_for_status()
        except httpx.TimeoutException:
            return ToolResult(
                success=False, output="",
                error=f"Request timed out: {url}"
            )
        except httpx.HTTPStatusError as e:
            return ToolResult(
                success=False, output="",
                error=f"HTTP {e.response.status_code} for {url}"
            )
        except Exception as e:
            return ToolResult(
                success=False, output="",
                error=f"Fetch failed: {e}"
            )

        content_type = resp.headers.get("content-type", "")
        if "text/html" not in content_type and "text/plain" not in content_type:
            return ToolResult(
                success=False, output="",
                error=f"Cannot process content type: {content_type}. Only text/html and text/plain are supported."
            )

        text = self._extract_text(resp.text, url)

        # Truncate
        MAX_CHARS = 15_000
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS] + (
                f"\n\n... [truncated {len(text) - MAX_CHARS:,} "
                f"chars from {url}]"
            )

        return ToolResult(
            success=True,
            output=f"Content from: {url}\n\n{text}",
        )

    # ── Sub-agent tools ──────────────────────────────────────────────────

    def _tool_spawn_subagent(self, task: str, skill: str = "",
                             model: str = "") -> ToolResult:
        """Spawn a sub-agent to work on a task in parallel."""
        if not self._sub_agent_mgr:
            return ToolResult(
                success=False, output="",
                error="SubAgentManager not available. "
                      "Ensure agent_controller is used.",
            )
        try:
            agent_id = self._sub_agent_mgr.spawn(
                task=task,
                skill_prompt=skill,
                model=model or None,
            )
            return ToolResult(
                success=True,
                output=(
                    f"Sub-agent spawned: {agent_id}\n"
                    f"Status: running\n"
                    f"Active sub-agents: {self._sub_agent_mgr.active_count}\n\n"
                    f"Use collect_subagent('{agent_id}') to retrieve results, "
                    f"or list_subagents() to check all statuses."
                ),
            )
        except RuntimeError as e:
            return ToolResult(
                success=False, output="",
                error=f"Cannot spawn sub-agent: {e}",
            )

    def _tool_collect_subagent(self, agent_id: str,
                                timeout: float = 300.0) -> ToolResult:
        """Collect results from a spawned sub-agent."""
        if not self._sub_agent_mgr:
            return ToolResult(
                success=False, output="",
                error="SubAgentManager not available.",
            )
        result = self._sub_agent_mgr.collect(agent_id, timeout=timeout)
        if result.success:
            lines = [
                f"Sub-agent {agent_id} completed successfully.",
                f"Tool calls: {result.tool_call_count}",
                f"",
                f"Result:",
                result.result or "(empty)",
            ]
            return ToolResult(success=True, output="\n".join(lines))
        else:
            return ToolResult(
                success=False,
                output=f"Sub-agent {agent_id} failed: {result.error}",
                error=result.error,
            )

    def _tool_list_subagents(self) -> ToolResult:
        """List all sub-agents and their statuses."""
        if not self._sub_agent_mgr:
            return ToolResult(
                success=False, output="",
                error="SubAgentManager not available.",
            )
        agents = self._sub_agent_mgr.list_all()
        if not agents:
            return ToolResult(success=True, output="No sub-agents.")

        lines = [f"Sub-agents ({len(agents)} total):", ""]
        for a in agents:
            status_icon = {"running": "🔄", "done": "✅",
                           "failed": "❌", "cancelled": "⏹️"}.get(a.status, "❓")
            lines.append(
                f"  {status_icon} {a.id} — {a.status} "
                f"(tool_calls={a.tool_call_count})"
            )
        return ToolResult(success=True, output="\n".join(lines))

    @staticmethod
    def _extract_text(html_text: str, url: str = "") -> str:
        """Strip HTML down to readable text."""

        class _TextExtractor(html.parser.HTMLParser):
            def __init__(self):
                super().__init__()
                self.parts: list[str] = []
                self._skip = False
                self._skip_tags = {"script", "style", "noscript", "iframe",
                                   "nav", "footer", "header", "aside"}
                self._block_tags = {"div", "p", "h1", "h2", "h3", "h4", "h5",
                                    "h6", "li", "tr", "section", "article",
                                    "pre", "blockquote", "table", "ul", "ol",
                                    "dl", "br", "hr"}

            def handle_starttag(self, tag, attrs):
                tag = tag.lower()
                if tag in self._skip_tags:
                    self._skip = True
                elif tag in self._block_tags:
                    self.parts.append("\n")

            def handle_endtag(self, tag):
                tag = tag.lower()
                if tag in self._skip_tags:
                    self._skip = False
                elif tag in self._block_tags:
                    self.parts.append("\n")

            def handle_data(self, data):
                if not self._skip:
                    text = data.strip()
                    if text:
                        self.parts.append(text + " ")

        try:
            extractor = _TextExtractor()
            extractor.feed(html_text)
            raw = "".join(extractor.parts)
        except Exception:
            # Fallback: regex strip
            raw = re.sub(r'<script[^>]*>.*?</script>', '', html_text, flags=re.DOTALL | re.IGNORECASE)
            raw = re.sub(r'<style[^>]*>.*?</style>', '', raw, flags=re.DOTALL | re.IGNORECASE)
            raw = re.sub(r'<[^>]+>', ' ', raw)
            raw = html.unescape(raw)

        # Collapse whitespace
        raw = re.sub(r'[ \t]+', ' ', raw)
        raw = re.sub(r'\n{3,}', '\n\n', raw)
        return raw.strip()


# ── Factory ──────────────────────────────────────────────────────────────────

def create_tool_executor(workspace_dir: str | None = None) -> ToolExecutor:
    """Create a tool executor with the given workspace."""
    from .config import AgentConfig
    if workspace_dir:
        cfg = AgentConfig(workspace_dir=workspace_dir)
        return ToolExecutor(cfg)
    return ToolExecutor()
