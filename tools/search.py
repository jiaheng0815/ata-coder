"""
Search tools — grep, glob, list_dir.

Extracted from ``executor.py`` as part of the planned split
(target ≤400 lines per module).  Provides filesystem search
operations with thread-pool offloading for grep.

Requires the host class (``ToolExecutor``) to provide:
- ``self._resolve_path(path)`` → Path
- ``self.workspace`` — resolved Path
- ``self.SKIP_DIRS`` — set[str]
- ``self.SKIP_DIR_SUFFIXES`` — tuple[str]
- ``self.MAX_GREP_RESULTS`` — int
- ``self.MAX_GREP_PER_FILE`` — int
- ``self._run_in_thread(func)`` — awaitable thread-pool executor
"""

import fnmatch
import logging
import os
import re

from .result import ToolResult

logger = logging.getLogger(__name__)


class SearchToolsMixin:
    """File search: grep, glob, list_dir."""

    async def _tool_grep(
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

        # Run filesystem walk + file reads in thread pool to avoid
        # blocking the asyncio event loop on large codebases.
        def _do_grep():
            results: list[str] = []
            total_matches = 0
            for root, dirs, files in os.walk(search_dir):
                dirs[:] = [
                    d for d in dirs
                    if not d.startswith(".")
                    and d not in self.SKIP_DIRS
                    and not d.endswith(self.SKIP_DIR_SUFFIXES)
                ]
                for fname in files:
                    full_path = os.path.join(root, fname)
                    try:
                        rel_path = os.path.relpath(full_path, self.workspace)
                    except ValueError:
                        rel_path = full_path  # path on different drive (Windows)
                    # Match glob against the relative path so patterns
                    # like "src/**/*.ts" work (fnmatch against bare fname
                    # would miss those).
                    if glob and not fnmatch.fnmatch(rel_path, glob):
                        continue

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
                        return results, total_matches

                if len(results) >= self.MAX_GREP_RESULTS:
                    break
            return results, total_matches

        results, total_matches = await self._run_in_thread(_do_grep)

        if not results:
            return ToolResult(
                success=True,
                output=f"No matches found for pattern: {pattern}",
            )
        return ToolResult(
            success=True,
            output=f"Found {total_matches} matches:\n\n" + "\n".join(results),
        )

    async def _tool_glob(
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

        # Auto-add **/ prefix for recursive matching if not already present
        if "**" not in pattern:
            pattern = f"**/{pattern}"
        search_pattern = str(search_dir / pattern)
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

    async def _tool_list_dir(
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
                    try:
                        size = os.path.getsize(fp)
                    except OSError:
                        size = 0  # broken symlink, permission denied, etc.
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
