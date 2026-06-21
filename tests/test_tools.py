# -*- coding: utf-8 -*-
"""Tests for the tool system (tool definitions, executor, results)."""

import tempfile
from pathlib import Path

import pytest
from ata_coder.tools import (
    TOOL_DEFINITIONS,
    ToolExecutor,
    ToolResult,
)
from ata_coder.config import AgentConfig


# ═══════════════════════════════════════════════════════════════════════════════
# ToolResult
# ═══════════════════════════════════════════════════════════════════════════════

class TestToolResult:
    @pytest.mark.asyncio
    async def test_success_to_message(self):
        r = ToolResult(success=True, output="hello")
        assert r.to_message() == "hello"

    @pytest.mark.asyncio
    async def test_failure_to_message(self):
        r = ToolResult(success=False, output="partial", error="not found")
        assert "Error: not found" in r.to_message()
        assert "partial" in r.to_message()

    @pytest.mark.asyncio
    async def test_to_tool_result(self):
        r = ToolResult(success=True, output="done")
        tr = r.to_tool_result("call-123")
        assert tr["role"] == "tool"
        assert tr["tool_call_id"] == "call-123"
        assert tr["content"] == "done"


# ═══════════════════════════════════════════════════════════════════════════════
# Tool definitions
# ═══════════════════════════════════════════════════════════════════════════════

class TestToolDefinitions:
    @pytest.mark.asyncio
    async def test_all_required_tools_present(self):
        names = {t["function"]["name"] for t in TOOL_DEFINITIONS}
        expected = {
            "read_file", "write_file", "edit_file", "rename_symbol", "run_shell",
            "grep", "glob", "list_dir", "web_search", "web_fetch",
            "spawn_subagent", "collect_subagent", "list_subagents",
            "mcp_search", "analyze_image",
        }
        assert names == expected

    @pytest.mark.asyncio
    async def test_each_tool_has_params(self):
        for tool in TOOL_DEFINITIONS:
            fn = tool["function"]
            assert "name" in fn
            assert "description" in fn
            assert "parameters" in fn


# ═══════════════════════════════════════════════════════════════════════════════
# ToolExecutor — basics
# ═══════════════════════════════════════════════════════════════════════════════

class TestToolExecutorBasics:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.executor = ToolExecutor(AgentConfig(workspace_dir=self.tmp))

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_unknown_tool(self):
        result = await self.executor.execute("nonexistent_tool", {})
        assert not result.success
        assert "Unknown" in result.error or "unknown" in result.error.lower()

    @pytest.mark.asyncio
    async def test_workspace_resolution(self):
        assert self.executor.workspace == Path(self.tmp).resolve()

    @pytest.mark.asyncio
    async def test_resolve_relative_path(self):
        p = self.executor._resolve_path("foo.py")
        assert p == self.executor.workspace / "foo.py"

    @pytest.mark.asyncio
    async def test_resolve_absolute_path(self):
        abs_path = str(Path(self.tmp) / "bar.py")
        p = self.executor._resolve_path(abs_path)
        assert p == Path(abs_path)


# ═══════════════════════════════════════════════════════════════════════════════
# ToolExecutor — read_file
# ═══════════════════════════════════════════════════════════════════════════════

class TestToolReadFile:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.executor = ToolExecutor(AgentConfig(workspace_dir=self.tmp))

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_read_existing_file(self):
        f = Path(self.tmp) / "test.txt"
        f.write_text("line1\nline2\nline3\n", encoding="utf-8")
        result = await self.executor.execute("read_file", {"file_path": str(f)})
        assert result.success
        assert "line1" in result.output
        assert "line2" in result.output

    @pytest.mark.asyncio
    async def test_read_nonexistent_file(self):
        result = await self.executor.execute("read_file",
                                        {"file_path": str(Path(self.tmp) / "ghost.txt")})
        assert not result.success
        assert "not found" in result.error.lower()

    @pytest.mark.asyncio
    async def test_read_directory_fails(self):
        result = await self.executor.execute("read_file", {"file_path": self.tmp})
        assert not result.success

    @pytest.mark.asyncio
    async def test_read_with_offset(self):
        f = Path(self.tmp) / "nums.txt"
        f.write_text("\n".join(str(i) for i in range(1, 21)), encoding="utf-8")
        result = await self.executor.execute("read_file",
                                        {"file_path": str(f), "offset": 10, "limit": 5})
        assert result.success
        # lines 10-14
        assert "10" in result.output
        assert "14" in result.output

    @pytest.mark.asyncio
    async def test_read_caches_file(self):
        f = Path(self.tmp) / "cache_test.txt"
        f.write_text("cached content\n", encoding="utf-8")
        str(f.resolve())
        # First read
        r1 = await self.executor.execute("read_file", {"file_path": str(f)})
        assert r1.success
        # Second read — should be from cache
        r2 = await self.executor.execute("read_file", {"file_path": str(f)})
        assert r2.success
        assert "[cached]" in r2.output


# ═══════════════════════════════════════════════════════════════════════════════
# ToolExecutor — write_file
# ═══════════════════════════════════════════════════════════════════════════════

class TestToolWriteFile:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.executor = ToolExecutor(AgentConfig(workspace_dir=self.tmp))

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_write_new_file(self):
        fp = str(Path(self.tmp) / "new.txt")
        result = await self.executor.execute("write_file",
                                        {"file_path": fp, "content": "hello world"})
        assert result.success
        assert Path(fp).read_text(encoding="utf-8") == "hello world"

    @pytest.mark.asyncio
    async def test_overwrite_existing_file(self):
        fp = Path(self.tmp) / "existing.txt"
        fp.write_text("old", encoding="utf-8")
        await self.executor.execute("write_file",
                               {"file_path": str(fp), "content": "new"})
        assert fp.read_text(encoding="utf-8") == "new"

    @pytest.mark.asyncio
    async def test_creates_parent_directories(self):
        fp = str(Path(self.tmp) / "a" / "b" / "c.txt")
        result = await self.executor.execute("write_file",
                                        {"file_path": fp, "content": "nested"})
        assert result.success
        assert Path(fp).read_text(encoding="utf-8") == "nested"


# ═══════════════════════════════════════════════════════════════════════════════
# ToolExecutor — edit_file
# ═══════════════════════════════════════════════════════════════════════════════

class TestToolEditFile:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.executor = ToolExecutor(AgentConfig(workspace_dir=self.tmp))

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_simple_replacement(self):
        fp = Path(self.tmp) / "edit.txt"
        fp.write_text("hello world", encoding="utf-8")
        result = await self.executor.execute("edit_file", {
            "file_path": str(fp),
            "old_string": "hello",
            "new_string": "hi",
        })
        assert result.success
        assert fp.read_text(encoding="utf-8") == "hi world"

    @pytest.mark.asyncio
    async def test_duplicate_string_rejected(self):
        """edit_file rejects old_string that appears multiple times (safety)."""
        fp = Path(self.tmp) / "multi.txt"
        fp.write_text("foo bar foo", encoding="utf-8")
        result = await self.executor.execute("edit_file", {
            "file_path": str(fp),
            "old_string": "foo",
            "new_string": "baz",
        })
        assert not result.success
        assert "2 times" in result.error or "unique" in result.error.lower()

    @pytest.mark.asyncio
    async def test_old_string_not_found(self):
        fp = Path(self.tmp) / "edit2.txt"
        fp.write_text("abc", encoding="utf-8")
        result = await self.executor.execute("edit_file", {
            "file_path": str(fp),
            "old_string": "xyz",
            "new_string": "nope",
        })
        assert not result.success


# ═══════════════════════════════════════════════════════════════════════════════
# ToolExecutor — run_shell
# ═══════════════════════════════════════════════════════════════════════════════

class TestToolRunShell:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.executor = ToolExecutor(AgentConfig(workspace_dir=self.tmp))

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_simple_command(self):
        result = await self.executor.execute("run_shell",
                                        {"command": "echo hello"})
        assert result.success
        assert "hello" in result.output

    @pytest.mark.asyncio
    async def test_command_with_stderr(self):
        result = await self.executor.execute("run_shell",
                                        {"command": "echo err >&2"})
        assert result.success

    @pytest.mark.asyncio
    async def test_blocked_command(self):
        """Command not in allowed list should be blocked."""
        result = await self.executor.execute("run_shell",
                                        {"command": "exit 1"})
        assert not result.success


# ═══════════════════════════════════════════════════════════════════════════════
# ToolExecutor — grep
# ═══════════════════════════════════════════════════════════════════════════════

class TestToolGrep:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.executor = ToolExecutor(AgentConfig(workspace_dir=self.tmp))

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_find_pattern(self):
        f = Path(self.tmp) / "grep_test.py"
        f.write_text("def hello():\n    return 'world'\n", encoding="utf-8")
        result = await self.executor.execute("grep",
                                        {"pattern": "def hello", "path": self.tmp})
        assert result.success
        assert "def hello" in result.output

    @pytest.mark.asyncio
    async def test_no_match(self):
        result = await self.executor.execute("grep",
                                        {"pattern": "zzz_nonexistent_zzz", "path": self.tmp})
        assert result.success
        assert "No matches" in result.output or result.output.strip() == ""

    @pytest.mark.asyncio
    async def test_case_insensitive(self):
        f = Path(self.tmp) / "case_test.py"
        f.write_text("HELLO WORLD\n", encoding="utf-8")
        result = await self.executor.execute("grep",
                                        {"pattern": "hello", "path": self.tmp,
                                         "case_sensitive": False})
        assert result.success
        assert "HELLO" in result.output


# ═══════════════════════════════════════════════════════════════════════════════
# ToolExecutor — glob
# ═══════════════════════════════════════════════════════════════════════════════

class TestToolGlob:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.executor = ToolExecutor(AgentConfig(workspace_dir=self.tmp))

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_find_py_files(self):
        (Path(self.tmp) / "a.py").touch()
        (Path(self.tmp) / "b.py").touch()
        (Path(self.tmp) / "c.txt").touch()
        result = await self.executor.execute("glob",
                                        {"pattern": "*.py", "path": self.tmp})
        assert result.success
        assert "a.py" in result.output
        assert "b.py" in result.output
        assert "c.txt" not in result.output

    @pytest.mark.asyncio
    async def test_recursive_glob(self):
        sub = Path(self.tmp) / "sub"
        sub.mkdir()
        (sub / "deep.py").touch()
        result = await self.executor.execute("glob",
                                        {"pattern": "**/*.py", "path": self.tmp})
        assert result.success
        assert "deep.py" in result.output


# ═══════════════════════════════════════════════════════════════════════════════
# ToolExecutor — list_dir
# ═══════════════════════════════════════════════════════════════════════════════

class TestToolListDir:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.executor = ToolExecutor(AgentConfig(workspace_dir=self.tmp))

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_list_directory(self):
        (Path(self.tmp) / "a.py").touch()
        (Path(self.tmp) / "b.py").touch()
        result = await self.executor.execute("list_dir", {"path": self.tmp})
        assert result.success
        assert "a.py" in result.output
        assert "b.py" in result.output


# ═══════════════════════════════════════════════════════════════════════════════
# ToolExecutor — output caps
# ═══════════════════════════════════════════════════════════════════════════════

class TestToolOutputCaps:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.executor = ToolExecutor(AgentConfig(workspace_dir=self.tmp))

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_large_output_truncated(self):
        # Set a low cap for testing
        self.executor.MAX_OUTPUT_CHARS = 100
        f = Path(self.tmp) / "big.txt"
        f.write_text("x" * 5000, encoding="utf-8")
        result = await self.executor.execute("read_file", {"file_path": str(f)})
        assert result.success
        assert "[cached]" in result.output or "truncated" in result.output.lower() or len(result.output) <= 200


# ═══════════════════════════════════════════════════════════════════════════════
# ToolExecutor — edit callback
# ═══════════════════════════════════════════════════════════════════════════════

class TestToolEditCallback:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.executor = ToolExecutor(AgentConfig(workspace_dir=self.tmp))

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_edit_callback_invoked(self):
        calls = []

        def cb(file_path, old_content):
            calls.append((file_path, old_content))

        self.executor.on_edit(cb)

        fp = Path(self.tmp) / "cb_test.txt"
        fp.write_text("original", encoding="utf-8")
        await self.executor.execute("edit_file", {
            "file_path": str(fp),
            "old_string": "original",
            "new_string": "modified",
        })
        assert len(calls) == 1
        assert calls[0][0] == str(fp)
        assert calls[0][1] == "original"

    @pytest.mark.asyncio
    async def test_edit_callback_not_called_on_read(self):
        calls = []

        def cb(*args):
            calls.append(1)

        self.executor.on_edit(cb)
        f = Path(self.tmp) / "read.txt"
        f.write_text("data", encoding="utf-8")
        await self.executor.execute("read_file", {"file_path": str(f)})
        # edit callback shouldn't fire on reads
        assert len(calls) == 0
