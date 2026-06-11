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

import logging
import os
import re
import subprocess
import fnmatch
from pathlib import Path
from typing import Any, Callable

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
]


# ── Tool implementations ─────────────────────────────────────────────────────

class ToolExecutor:
    """Executes tool calls and manages workspace context."""

    def __init__(self, config: AgentConfig | None = None):
        self.config = config or AgentConfig()
        self.workspace = Path(self.config.workspace_dir).resolve()
        self._edit_callback: Callable[[str, str], None] | None = None

    def on_edit(self, callback: Callable[[str, str], None]) -> None:
        """Register callback for edit notifications: callback(file_path, old_content)."""
        self._edit_callback = callback

    def _notify_edit(self, file_path: str, old_content: str) -> None:
        """Notify the UI of a file edit for diff display."""
        if self._edit_callback:
            try:
                self._edit_callback(file_path, old_content)
            except Exception:
                pass

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

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except Exception as e:
            return ToolResult(
                success=False, output="", error=f"Cannot read file: {e}"
            )

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
        glob_filter: str | None = None,
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
        max_results = 100
        max_per_file = 20

        for root, dirs, files in os.walk(search_dir):
            dirs[:] = [
                d for d in dirs
                if not d.startswith(".")
                and d not in ("node_modules", "__pycache__", ".git", "venv", ".venv",
                              "dist", "build", "target", ".next")
            ]
            for fname in files:
                if glob_filter and not fnmatch.fnmatch(fname, glob_filter):
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
                        if len(matches_in_file) >= max_per_file:
                            matches_in_file.append("  ... (truncated)")
                            break

                if matches_in_file:
                    results.append(f"{rel_path}:")
                    results.extend(matches_in_file)

                if len(results) >= max_results:
                    results.append("... (result limit reached)")
                    break

            if len(results) >= max_results:
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
                dirs[:] = [d for d in dirs if not d.startswith(".")]
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


# ── Factory ──────────────────────────────────────────────────────────────────

def create_tool_executor(workspace_dir: str | None = None) -> ToolExecutor:
    """Create a tool executor with the given workspace."""
    from .config import AgentConfig
    if workspace_dir:
        cfg = AgentConfig(workspace_dir=workspace_dir)
        return ToolExecutor(cfg)
    return ToolExecutor()
