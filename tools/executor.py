"""
Tool system for the ATA Coder.

📐 **Planned split** (currently ~1068 lines — target ≤400 per module):
  - ``tools/file_ops.py``   — read_file, write_file, edit_file, rename_file
  - ``tools/shell_exec.py`` — run_shell, shell streaming, timeout mgmt
  - ``tools/search.py``     — grep, glob, list_dir, web_search, web_fetch
  The executor module coordinates tool dispatch; tool implementations
  are mixin classes that can be extracted independently.  New tools
  should be added to the target sub-module from now on.

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

import asyncio
import logging
import os
import re
import shlex
import shutil
import fnmatch
from pathlib import Path
from typing import Any, Callable

# Cached reference for __del__ — avoids "import asyncio" during interpreter
# shutdown, which fails with "ImportError: sys.meta_path is None".
_asyncio_get_running_loop = asyncio.get_running_loop


from ..config import AgentConfig
from .result import ToolResult
from .file_ops import FileOpsMixin
from .shell_exec import ShellExecMixin
from .search import SearchToolsMixin
from .web import WebToolsMixin
from .subagent import SubAgentToolsMixin

logger = logging.getLogger(__name__)


# ── Tool result type ─────────────────────────────────────────────────────────

class ToolExecutor(FileOpsMixin, ShellExecMixin, SearchToolsMixin, WebToolsMixin, SubAgentToolsMixin):
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
        ".mypy_cache", ".ruff_cache", ".tox", ".idea", ".vs",
        ".vscode", "bower_components", ".terraform", ".eggs",
        "htmlcov", ".coverage", "__pypackages__",
    }
    # Glob-suffix patterns for directories to skip (checked via fnmatch / endswith)
    SKIP_DIR_SUFFIXES = (".egg-info",)

    def __init__(self, config: AgentConfig | None = None):
        self.config = config or AgentConfig()
        self.workspace = Path(self.config.workspace_dir).resolve()
        self._mcp = None  # set via set_mcp_client()
        self._edit_callback: Callable[[str, str], None] | None = None
        # File read cache: path → (mtime, cached_at, content).
        self._file_cache: dict[str, tuple[float, float, str]] = {}
        self._file_cache_max_entries = 200
        self._FILE_CACHE_TTL = 30.0  # seconds before a cache entry is revalidated
        self._cache_dir: Path | None = None
        # Sub-agent manager (set by agent)
        self._sub_agent_mgr: Any = None
        # Pre-built handler map (validated at init time)
        self._handlers: dict[str, Callable] = self._build_handlers()
        # Streaming callback: set by agent to receive real-time output chunks
        self._stream_cb: Callable[[str, str], None] | None = None  # (tool_name, chunk)

    def set_stream_callback(self, cb: Callable[[str, str], None] | None) -> None:
        """Set callback for real-time tool output streaming.

        ``cb(tool_name, chunk)`` is called with incremental output chunks
        during long-running tool execution (e.g. run_shell).
        Set to None to disable streaming.
        """
        self._stream_cb = cb

    # Explicit tool handler names — each must have a corresponding
    # _tool_<name> method on this class or a mixin.
    TOOL_NAMES: tuple[str, ...] = (
        "read_file", "write_file", "edit_file", "rename_symbol",
        "run_shell", "grep", "glob", "list_dir",
        "web_search", "web_fetch",
        "spawn_subagent", "collect_subagent", "list_subagents",
        "mcp_search", "analyze_image",
    )

    def _build_handlers(self) -> dict[str, Callable]:
        """Build a dispatch table: tool_name → handler callable.

        Uses the explicit TOOL_NAMES list — only these methods are
        registered, preventing accidental registration of mixin
        properties or helper objects.
        """
        handlers: dict[str, Callable] = {}
        for name in self.TOOL_NAMES:
            attr = getattr(self, f"_tool_{name}", None)
            if callable(attr):
                handlers[name] = attr
        return handlers

    def set_sub_agent_manager(self, mgr: Any) -> None:
        """Set the SubAgentManager for spawn/collect sub-agent tool support."""
        self._sub_agent_mgr = mgr

    def set_mcp_client(self, mcp: Any) -> None:
        """Set the MCPClient for mcp_search tool support."""
        self._mcp = mcp

    def setup_file_cache(self, cache_dir: str | Path) -> None:
        """Create session cache directory. Call once before running the agent."""
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def clear_file_cache(self) -> None:
        """Remove all cached files and the cache directory."""
        self._file_cache.clear()
        if self._cache_dir and self._cache_dir.exists():
            shutil.rmtree(self._cache_dir, ignore_errors=True)
            self._cache_dir = None

    @staticmethod
    def _cache_safe_name(resolved_path: str) -> str:
        """Normalise a resolved path to a cross-platform safe cache filename.

        Strips the Windows drive letter and replaces separators with underscores
        so the same path always maps to the same cache file name.
        """
        if len(resolved_path) >= 2 and resolved_path[1] == ":":
            resolved_path = resolved_path[2:]  # strip "C:"
        return resolved_path.lstrip("\\/").replace("\\", "_").replace("/", "_")

    def _invalidate_cache(self, file_path: str) -> None:
        """Invalidate the in-memory and on-disk cache for *file_path*."""
        resolved = str(Path(file_path).resolve())
        self._file_cache.pop(resolved, None)
        if self._cache_dir:
            safe = self._cache_safe_name(resolved)
            cached = self._cache_dir / safe
            if cached.exists():
                try:
                    cached.unlink()
                except Exception:
                    pass

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
        """Safety net: ensure httpx client is closed on GC.

        Only calls close() outside an active asyncio event loop — during
        interpreter shutdown the loop may already be closed, and touching
        it from __del__ triggers spurious warnings/errors.

        Uses a cached reference to asyncio.get_running_loop (the module-level
        import at the top of this file) to avoid ``import asyncio`` inside
        __del__, which fails with ``ImportError: sys.meta_path is None``
        during interpreter shutdown.
        """
        try:
            _asyncio_get_running_loop()
        except RuntimeError:
            # No running loop — safe to close synchronously
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
        """Resolve a file path relative to workspace.

        Raises ValueError if the resolved path escapes the workspace via
        path traversal (e.g., ``../../etc/passwd``).
        """
        p = Path(file_path)
        if not p.is_absolute():
            p = self.workspace / p
        resolved = p.resolve()
        try:
            resolved.relative_to(self.workspace.resolve())
        except ValueError:
            raise ValueError(
                f"Path traversal blocked: {file_path} → {resolved} "
                f"is outside workspace {self.workspace}"
            )
        return resolved

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

    # The agent layer handles output truncation via max_message_output_chars
    # (default 8k).  We leave a generous safety ceiling here only as a
    # backstop for edge cases where the agent's truncation is bypassed.
    MAX_OUTPUT_CHARS = 500_000

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        """Dispatch a tool call to the appropriate handler.

        Output truncation is handled by the agent layer (max_message_output_chars).
        """
        handler = self._handlers.get(tool_name)
        if handler is None:
            return ToolResult(
                success=False,
                output="",
                error=f"Unknown tool: {tool_name}",
            )
        try:
            result = await handler(**arguments)
        except Exception as e:
            logger.exception("Tool %s failed", tool_name)
            return ToolResult(
                success=False, output="", error=f"{type(e).__name__}: {e}"
            )

        # Safety ceiling (rarely hit - agent does the real truncation)
        if len(result.output) > self.MAX_OUTPUT_CHARS:
            original_len = len(result.output)
            suffix = f" ... [truncated {original_len - self.MAX_OUTPUT_CHARS:,} chars]"
            result.output = result.output[:self.MAX_OUTPUT_CHARS] + suffix
            logger.warning(
                "Tool %s output exceeded safety ceiling: %d -> %d chars",
                tool_name, original_len, self.MAX_OUTPUT_CHARS,
            )

        return result

    # ── File tools ───────────────────────────────────────────────────────────

    # ── Output limits (prevent token bloat from large file reads) ──────
    MAX_READ_LINES = 2000        # auto-truncate reads without an explicit limit
    MAX_READ_CHARS = 80_000      # hard cap on output chars (~20k tokens)
    # _tool_read_file / _tool_write_file / _tool_edit_file → FileOpsMixin (tools/file_ops.py)

    # ── Rename symbol (AST-aware) ────────────────────────────────────────────

    async def _tool_rename_symbol(
        self, file_path: str, old_name: str, new_name: str,
        symbol_type: str = "variable"
    ) -> ToolResult:
        """Safely rename a Python symbol using libcst AST matching.
        Never touches strings, comments, or imports of the same name.
        """
        if old_name == new_name:
            return ToolResult(success=False, output="", error="Names are identical.")
        if not old_name.isidentifier() or not new_name.isidentifier():
            return ToolResult(success=False, output="", error="Names must be valid Python identifiers.")

        path = self._resolve_path(file_path)
        if not path.exists():
            return ToolResult(success=False, output="", error=f"File not found: {path}")
        if path.suffix not in (".py", ".pyi"):
            return ToolResult(success=False, output="", error="rename_symbol only works with Python files.")

        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Cannot read file: {e}")

        try:
            import libcst as cst
        except ImportError:
            return ToolResult(success=False, output="", error="libcst not installed. Run: pip install libcst")

        try:
            tree = cst.parse_module(content)
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Cannot parse Python file: {e}")

        # Choose the right renamer for the symbol type
        if symbol_type in ("function", "method"):
            renamer = _FunctionRenamer(old_name, new_name)
        elif symbol_type == "class":
            renamer = _ClassRenamer(old_name, new_name)
        else:
            renamer = _VariableRenamer(old_name, new_name)

        new_tree = tree.visit(renamer)
        if not renamer.changes:
            return ToolResult(success=False, output="", error=f"Symbol '{old_name}' not found in file.")

        new_content = new_tree.code
        old_content = content

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_content)
            self._notify_edit(str(path), old_content)
            self._invalidate_cache(str(path))
            return ToolResult(
                success=True,
                output=f"Renamed {renamer.changes} occurrence(s) of '{old_name}' → '{new_name}' in {path}",
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Cannot write file: {e}")

    # _tool_run_shell → ShellExecMixin (tools/shell_exec.py)
    # _tool_grep / _tool_glob / _tool_list_dir → SearchToolsMixin (tools/search.py)


# ── Factory ──────────────────────────────────────────────────────────────────

def create_tool_executor(workspace_dir: str | None = None) -> ToolExecutor:
    """Create a tool executor with the given workspace."""
    from ..config import AgentConfig
    if workspace_dir:
        cfg = AgentConfig(workspace_dir=workspace_dir)
        return ToolExecutor(cfg)
    return ToolExecutor()


# ── CST Renamers (for rename_symbol tool) ───────────────────────────────────
# These use libcst to safely rename symbols without touching strings or comments.

class _SymbolRenamer:
    """Base class for CST symbol renamers — shared leave_Name/leave_Call logic."""

    def __init__(self, old_name: str, new_name: str):
        self.old = old_name
        self.new = new_name
        self.changes = 0

    def leave_Name(self, original_node, updated_node):
        import libcst as cst
        if isinstance(updated_node, cst.Name) and updated_node.value == self.old:
            self.changes += 1
            return updated_node.with_changes(value=self.new)
        return updated_node

    def leave_Call(self, original_node, updated_node):
        import libcst as cst
        if isinstance(updated_node.func, cst.Name) and updated_node.func.value == self.old:
            self.changes += 1
            return updated_node.with_changes(func=cst.Name(value=self.new))
        return updated_node


class _VariableRenamer(_SymbolRenamer):
    """Rename variable references — names only, not touching strings/comments."""


class _FunctionRenamer(_SymbolRenamer):
    """Rename function/method definitions and calls."""

    def leave_FunctionDef(self, original_node, updated_node):
        import libcst as cst
        if updated_node.name.value == self.old:
            self.changes += 1
            return updated_node.with_changes(name=cst.Name(value=self.new))
        return updated_node


class _ClassRenamer(_SymbolRenamer):
    """Rename class definitions and constructor calls."""

    def leave_ClassDef(self, original_node, updated_node):
        import libcst as cst
        if updated_node.name.value == self.old:
            self.changes += 1
            return updated_node.with_changes(name=cst.Name(value=self.new))
        return updated_node
