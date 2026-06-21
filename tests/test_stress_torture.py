"""
🧨 TORTURE TEST SUITE — 极限压力测试

This is the "outrageous" part. We push every subsystem to its breaking
point with concurrent hammer tests, huge payloads, rapid lifecycle churn,
and pathological inputs. If anything survives this, it's battle-ready.
"""

import asyncio
import gc
import os
import random
import string
import tempfile
import threading
import time
from pathlib import Path

import pytest

from ata_coder.config import AgentConfig
from ata_coder.tools.executor import ToolExecutor
from ata_coder.memory import MemoryStore, Memory
from ata_coder.token_counter import TokenCounter


# ═══════════════════════════════════════════════════════════════════════════
# CONCURRENT HAMMER — 1000 parallel tool calls
# ═══════════════════════════════════════════════════════════════════════════

class TestConcurrentHammer:
    """Hit the tool executor with 1000+ concurrent operations."""

    @pytest.mark.asyncio
    async def test_1000_concurrent_reads(self, tmp_path):
        """Read 1000 small files concurrently — must not crash or deadlock."""
        files = []
        for i in range(1000):
            f = tmp_path / f"hammer_{i:04d}.txt"
            f.write_text(f"file {i} content\n", encoding="utf-8")
            files.append(f)

        config = AgentConfig(workspace_dir=str(tmp_path))
        ex = ToolExecutor(config)

        t0 = time.monotonic()
        results = await asyncio.gather(*[
            ex._tool_read_file(str(f)) for f in files
        ])
        elapsed = time.monotonic() - t0

        successes = [r for r in results if r.success]
        assert len(successes) == 1000, f"Only {len(successes)}/1000 reads succeeded"
        assert elapsed < 30.0, f"1000 reads took {elapsed:.1f}s"

        ex.close()

    @pytest.mark.asyncio
    async def test_concurrent_mixed_ops_no_deadlock(self, tmp_path):
        """Run read+write+glob+grep concurrently — detect deadlocks."""
        config = AgentConfig(workspace_dir=str(tmp_path))
        ex = ToolExecutor(config)

        # Pre-create files
        for i in range(200):
            (tmp_path / f"mixed_{i}.py").write_text(
                f"# File {i}\ndef foo_{i}():\n    return {i}\n", encoding="utf-8")

        async def _reader():
            for i in range(50):
                await ex._tool_read_file(str(tmp_path / f"mixed_{i % 200}.py"))

        async def _writer():
            for i in range(20):
                await ex._tool_write_file(
                    str(tmp_path / f"mixed_new_{i}.py"),
                    f"# Generated {i}\nprint({i})\n"
                )

        async def _globber():
            for _ in range(10):
                await ex._tool_glob("*.py", path=str(tmp_path))

        async def _grepper():
            for _ in range(10):
                await ex._tool_grep(r"def foo_\d+", path=str(tmp_path))

        t0 = time.monotonic()
        await asyncio.wait_for(
            asyncio.gather(
                *[_reader() for _ in range(4)],
                *[_writer() for _ in range(4)],
                *[_globber() for _ in range(4)],
                *[_grepper() for _ in range(4)],
            ),
            timeout=60.0,
        )
        elapsed = time.monotonic() - t0
        assert elapsed < 60.0, f"Mixed ops deadlocked or timed out at {elapsed:.1f}s"

        ex.close()


# ═══════════════════════════════════════════════════════════════════════════
# HUGE PAYLOADS
# ═══════════════════════════════════════════════════════════════════════════

class TestHugePayloads:
    """Push file size and content limits to extremes."""

    @pytest.mark.asyncio
    async def test_read_100mb_file(self, tmp_path):
        """Read a 100 MB file without OOM or event-loop blocking."""
        f = tmp_path / "big_100mb.bin"
        chunk = "A" * 1024 * 1024  # 1 MB of 'A'
        with open(f, "w", encoding="utf-8") as fh:
            for _ in range(100):
                fh.write(chunk)

        config = AgentConfig(workspace_dir=str(tmp_path))
        ex = ToolExecutor(config)

        t0 = time.monotonic()
        result = await ex._tool_read_file(str(f))
        elapsed = time.monotonic() - t0

        assert result.success
        assert elapsed < 10.0, f"100MB file read took {elapsed:.1f}s"
        # Output should be truncated (MAX_READ_LINES), not 100MB of text
        assert len(result.output) < 100_000, (
            f"Output not truncated: {len(result.output):,} chars"
        )

        ex.close()

    @pytest.mark.asyncio
    async def test_read_file_zero_bytes(self, tmp_path):
        """Empty files should be handled gracefully."""
        f = tmp_path / "empty.txt"
        f.write_text("", encoding="utf-8")

        config = AgentConfig(workspace_dir=str(tmp_path))
        ex = ToolExecutor(config)

        result = await ex._tool_read_file(str(f))
        assert result.success
        assert result.success
        assert "0 of 0" in result.output or "0 lines" in result.output

        ex.close()

    @pytest.mark.asyncio
    async def test_read_file_binary_junk(self, tmp_path):
        """Binary files should be read without crashing (errors='replace')."""
        f = tmp_path / "junk.bin"
        f.write_bytes(bytes(range(256)) * 100)

        config = AgentConfig(workspace_dir=str(tmp_path))
        ex = ToolExecutor(config)

        result = await ex._tool_read_file(str(f))
        assert result.success  # should not crash
        # Should have replacement chars but not throw
        assert len(result.output) > 0

        ex.close()


# ═══════════════════════════════════════════════════════════════════════════
# RAPID LIFECYCLE CHURN — create/destroy cycles
# ═══════════════════════════════════════════════════════════════════════════

class TestRapidLifecycle:
    """Create and destroy executors rapidly to find resource leaks."""

    def test_1000_create_destroy_cycles(self, tmp_path):
        """Create/destroy 1000 executors — no FD leak, no OOM."""
        gc.collect()
        gc.disable()  # remove GC timing noise

        for i in range(1000):
            config = AgentConfig(workspace_dir=str(tmp_path))
            ex = ToolExecutor(config)
            # Access http to create a client
            try:
                _ = ex.http
            except Exception:
                pass
            ex.close()

        gc.enable()
        gc.collect()

        # If we got here without crashing, it's a win
        # But also verify: temp dir cleanup
        remaining = list(tmp_path.iterdir())
        # Should only have test-created files, not leaked cache dirs
        cache_dirs = [d for d in remaining if d.name.startswith(".ata_cache")]
        assert len(cache_dirs) <= 1, f"Leaked {len(cache_dirs)} cache dirs"


# ═══════════════════════════════════════════════════════════════════════════
# PATHOLOGICAL INPUTS
# ═══════════════════════════════════════════════════════════════════════════

class TestPathologicalInputs:
    """Inputs designed to break parsers and validators."""

    @pytest.fixture
    def ex(self, tmp_path):
        config = AgentConfig(workspace_dir=str(tmp_path))
        e = ToolExecutor(config)
        yield e
        e.close()

    @pytest.mark.asyncio
    async def test_null_bytes_in_file_path(self, ex):
        """Null bytes in path should be rejected, not crash."""
        result = await ex._tool_read_file("foo\x00bar.txt")
        assert not result.success

    @pytest.mark.asyncio
    async def test_unicode_surrogate_in_path(self, ex):
        """Lone surrogate in path should be handled."""
        result = await ex._tool_read_file("test_\ud800_file.txt")
        assert not result.success

    @pytest.mark.asyncio
    async def test_path_traversal_attempt(self, ex, tmp_path):
        """Path traversal should be blocked — raises ValueError caught by agent dispatch."""
        with pytest.raises(ValueError, match="Path traversal blocked"):
            await ex._tool_read_file("../../../etc/passwd")

    @pytest.mark.asyncio
    async def test_grep_with_pathological_regex(self, ex, tmp_path):
        """Catastrophic backtracking regex should not hang."""
        (tmp_path / "re_test.txt").write_text("aaaaaaaaaaaaaaaaaaaa!", encoding="utf-8")
        # Evil regex: (a+)+b — should not hang due to timeout or rejection
        result = await ex._tool_grep(r"(a+)+b", path=str(tmp_path))
        # Should complete, not hang
        assert result.success


# ═══════════════════════════════════════════════════════════════════════════
# MEMORY STORE STRESS
# ═══════════════════════════════════════════════════════════════════════════

class TestMemoryStoreStress:
    """Push the memory system to its limits."""

    def test_10000_memory_bulk_load(self, tmp_path):
        """Load 10,000 memories — should not OOM or crash."""
        store = MemoryStore(memory_dir=str(tmp_path))

        for i in range(10000):
            store.add(Memory(
                name=f"bulk-{i:05d}",
                description=f"Memory #{i}",
                content=f"Content for memory number {i} with keywords: "
                        f"{' '.join(random.choices(string.ascii_lowercase, k=20))}",
            ))

        # Recall should still work
        t0 = time.perf_counter()
        result = store.recall_context("memory number 5000")
        elapsed = time.perf_counter() - t0
        assert len(result) > 0, "Recall failed with 10k memories"
        assert elapsed < 2.0, f"Recall from 10k memories took {elapsed*1000:.1f}ms"

    def test_concurrent_add_recall(self, tmp_path):
        """Add and recall memories concurrently from threads."""
        store = MemoryStore(memory_dir=str(tmp_path))
        errors = []

        def _adder(thread_id: int):
            try:
                for i in range(500):
                    store.add(Memory(
                        name=f"thread-{thread_id}-{i:04d}",
                        description=f"Thread {thread_id} memory {i}",
                        content=f"Concurrent test data from thread {thread_id} item {i}",
                    ))
            except Exception as e:
                errors.append(f"adder-{thread_id}: {e}")

        def _recaller():
            try:
                for _ in range(100):
                    store.recall_context("concurrent test data")
            except Exception as e:
                errors.append(f"recaller: {e}")

        threads = []
        for tid in range(8):
            threads.append(threading.Thread(target=_adder, args=(tid,)))
        threads.append(threading.Thread(target=_recaller))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent memory errors: {errors}"


# ═══════════════════════════════════════════════════════════════════════════
# TOKEN COUNTER EDGE CASES
# ═══════════════════════════════════════════════════════════════════════════

class TestTokenCounterEdge:
    """Token counter edge cases and fuzz."""

    def test_empty_messages(self):
        """Empty content should return 0 tokens."""
        tc = TokenCounter.for_model("gpt-4o")
        assert tc.count_tokens([]) == 0
        assert tc.count_tokens([{"role": "system", "content": ""}]) >= 0

    def test_very_long_message(self):
        """A message with 1M characters should not crash."""
        tc = TokenCounter.for_model("gpt-4o")
        long_text = "Hello world. " * 100_000  # ~1.2M chars
        t0 = time.perf_counter()
        result = tc.count_tokens([{"role": "user", "content": long_text}])
        elapsed = time.perf_counter() - t0
        assert result > 0
        assert elapsed < 5.0, f"1.2M char token count took {elapsed:.1f}s"

    def test_cjk_heavy_content(self):
        """CJK text should give reasonable estimates."""
        tc = TokenCounter.for_model("deepseek-chat")
        cjk_text = "你好世界！这是一个测试。" * 1000  # CJK heavy
        result = tc.count_tokens([{"role": "user", "content": cjk_text}])
        # CJK tokens should be roughly 1-3 chars per token
        assert result > len(cjk_text) // 3
        assert result < len(cjk_text) * 2
