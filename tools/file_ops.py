"""
File operation tools — read, write, and edit files within the workspace.

Extracted from ``executor.py`` as part of the planned split
(target ≤400 lines per module).  This mixin provides the three
file-oriented tool handlers used by the agent:

- ``_tool_read_file``   — read a file with caching and line-range paging
- ``_tool_write_file``  — create / overwrite a file with diff preview
- ``_tool_edit_file``   — precise string replacement (AST-aware for Python)

All methods access shared state (workspace, file cache, edit callback)
through ``self``, which is resolved at runtime when the mixin is
combined with ``ToolExecutor``.
"""

import logging
import time as _time
from pathlib import Path
from typing import Any

from .result import ToolResult

logger = logging.getLogger(__name__)


class FileOpsMixin:
    """File read / write / edit tool handlers.

    Requires the host class to provide:
    - ``self.config`` — AgentConfig instance
    - ``self.workspace`` — resolved Path
    - ``self._resolve_path(file_path)`` → Path
    - ``self._ensure_in_workspace(path)`` → Path
    - ``self._file_cache`` — dict[str, tuple[float, float, str]]
    - ``self._file_cache_max_entries`` — int
    - ``self._FILE_CACHE_TTL`` — float (seconds)
    - ``self._cache_dir`` — Path | None
    - ``self._cache_safe_name(resolved_path)`` → str
    - ``self._notify_edit(file_path, old_content)``
    - ``self._invalidate_cache(file_path)``
    - ``self.MAX_READ_LINES`` — int
    - ``self.MAX_READ_CHARS`` — int
    """

    # ── Read file ──────────────────────────────────────────────────────────

    async def _tool_read_file(
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
            cached_mtime, cached_at, cached_content = self._file_cache[cache_key]
            age = _time.time() - cached_at
            if age > self._FILE_CACHE_TTL:
                # TTL expired — re-read from disk to catch external modifications
                pass  # fall through to needs_disk_read
            elif cached_mtime == current_mtime:
                # Refresh LRU position: pop and re-insert moves key to end
                self._file_cache.pop(cache_key)
                self._file_cache[cache_key] = (cached_mtime, _time.time(), cached_content)
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
            self._file_cache[cache_key] = (current_mtime, _time.time(), raw)
            # LRU eviction: dict preserves insertion order (Python 3.7+).
            # Cache hits move entries to the end, so the first keys are
            # always the least-recently-used ones.
            if len(self._file_cache) > self._file_cache_max_entries:
                overflow = len(self._file_cache) - self._file_cache_max_entries
                oldest = list(self._file_cache.keys())[:overflow]
                for k in oldest:
                    del self._file_cache[k]
            lines = raw.splitlines(keepends=True)
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            # Mirror to disk cache dir if configured
            if self._cache_dir:
                try:
                    safe_name = self._cache_safe_name(str(path.resolve()))
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
            header += ", truncated — use offset/limit to page"
        header += ")\n"
        return ToolResult(success=True, output=header + "\n".join(output_lines))

    # ── Write file ─────────────────────────────────────────────────────────

    async def _tool_write_file(
        self, file_path: str, content: str
    ) -> ToolResult:
        """Create or overwrite a file. Captures old content for diff display."""
        path = self._resolve_path(file_path)
        self._ensure_in_workspace(path)

        # Capture old content for diff (if file exists)
        old_content = ""
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    old_content = f.read()
            except Exception:
                pass

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            size = path.stat().st_size

            # Notify UI for diff display if overwriting
            if old_content:
                self._notify_edit(str(path), old_content)

            # Invalidate file cache so subsequent reads get fresh content
            self._invalidate_cache(str(path))

            return ToolResult(
                success=True,
                output=f"File written: {path} ({size} bytes, {content.count(chr(10))} lines)",
            )
        except Exception as e:
            return ToolResult(
                success=False, output="", error=f"Cannot write file: {e}"
            )

    # ── Edit file ─────────────────────────────────────────────────────────

    async def _tool_edit_file(
        self, file_path: str, old_string: str, new_string: str
    ) -> ToolResult:
        """Replace text in a file. Uses CST for Python (preserves formatting),
        falls back to text replacement for other languages."""
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

        # Store old content for diff display (via UI callback)
        old_content = content

        # ── CST-based edit for Python files ──────────────────────────────
        if path.suffix == ".py" or path.suffix == ".pyi":
            result = self._cst_edit(content, old_string, new_string, path)
            if result is not None:
                new_content = result
                try:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(new_content)
                    self._notify_edit(str(path), old_content)
                    self._invalidate_cache(str(path))
                    return ToolResult(
                        success=True,
                        output=f"File edited (AST): {path} (1 replacement)",
                    )
                except Exception as e:
                    return ToolResult(
                        success=False, output="", error=f"Cannot write file: {e}"
                    )
            # CST edit failed — fall through to text-based replacement

        # ── Text-based fallback ──────────────────────────────────────────
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

        new_content = content.replace(old_string, new_string, 1)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_content)

            self._notify_edit(str(path), old_content)
            self._invalidate_cache(str(path))

            return ToolResult(
                success=True,
                output=f"File edited: {path} (1 replacement)",
            )
        except Exception as e:
            return ToolResult(
                success=False, output="", error=f"Cannot write file: {e}"
            )

    @staticmethod
    def _cst_edit(content: str, old_str: str, new_str: str, path: Any) -> str | None:
        """Attempt a CST-based edit for Python files using libcst.

        Parses the file as a Concrete Syntax Tree, finds the node matching
        old_str, replaces it with new_str parsed as the same node type,
        and returns the formatted code. Preserves ALL formatting.

        Returns None if the edit cannot be performed via CST (fallback to text).
        """
        try:
            import libcst as cst
        except ImportError:
            return None  # libcst not installed — use text fallback

        try:
            tree = cst.parse_module(content)
        except Exception:
            return None  # Can't parse — fall back to text

        # Strategy: parse old_str as a statement, find it in the tree, replace
        try:
            old_module = cst.parse_module(old_str + "\n")
            if len(old_module.body) == 1:
                old_node = old_module.body[0]
                new_module = cst.parse_module(new_str + "\n")
                if len(new_module.body) == 1:
                    new_node = new_module.body[0]
                    transformer = FileOpsMixin._NodeReplacer(old_node, new_node)
                    new_tree = tree.visit(transformer)
                    if transformer.found:
                        return new_tree.code
        except Exception:
            pass

        # Strategy 2: try parsing old_str as a simple statement line
        try:
            old_module = cst.parse_module(old_str + "\n")
            if len(old_module.body) == 1:
                old_stmt = old_module.body[0]
                new_module = cst.parse_module(new_str + "\n")
                if len(new_module.body) == 1:
                    new_stmt = new_module.body[0]
                    transformer = FileOpsMixin._StatementReplacer(old_stmt, new_stmt)
                    new_tree = tree.visit(transformer)
                    if transformer.found:
                        return new_tree.code
        except Exception:
            pass

        return None  # All CST strategies failed — fall back to text

    # ── CST Transformers (libcst optional — guarded at class level) ──────

    try:
        import libcst as _cst_lib

        class _NodeReplacer(_cst_lib.CSTTransformer):
            """Replace a specific CST node with another, preserving all else."""

            def __init__(self, old_node: _cst_lib.CSTNode, new_node: _cst_lib.CSTNode):
                self.old_node = old_node
                self.new_node = new_node
                self.found = False

            def on_visit(self, node: _cst_lib.CSTNode) -> bool:
                if node.deep_equals(self.old_node) and not self.found:
                    self.found = True
                    return False
                return True

            def on_leave(self, original_node: _cst_lib.CSTNode, updated_node: _cst_lib.CSTNode) -> _cst_lib.CSTNode:
                if original_node.deep_equals(self.old_node) and self.found:
                    return self.new_node
                return updated_node

        class _StatementReplacer(_cst_lib.CSTTransformer):
            """Replace a statement within a body, matching by deep equality."""

            def __init__(self, old_stmt: _cst_lib.CSTNode, new_stmt: _cst_lib.CSTNode):
                self.old_stmt = old_stmt
                self.new_stmt = new_stmt
                self.found = False

            def leave_SimpleStatementLine(
                self, original_node: _cst_lib.SimpleStatementLine, updated_node: _cst_lib.SimpleStatementLine
            ):
                if not self.found and len(updated_node.body) == 1:
                    if updated_node.body[0].deep_equals(self.old_stmt):
                        self.found = True
                        return updated_node.with_changes(body=[self.new_stmt])
                return updated_node

    except ImportError:
        # libcst not installed — AST editing will fall back to text replacement
        _NodeReplacer = None   # type: ignore
        _StatementReplacer = None  # type: ignore
