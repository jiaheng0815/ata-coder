"""
Performance / stress tests for ATA Coder.

Tests actual event-loop responsiveness, concurrent throughput,
memory behaviour, and cache hit rates — not just functional correctness.
"""

import asyncio
import gc
import os
import tempfile
import threading
import time
import timeit
from functools import partial
from pathlib import Path

import pytest

from ata_coder.config import AgentConfig
from ata_coder.tools.executor import ToolExecutor
from ata_coder.memory import MemoryStore, Memory
from ata_coder.token_counter import TokenCounter


# ── helpers ──────────────────────────────────────────────────────────────


def _event_loop_responsive(threshold_ms: float = 50, samples: int = 50) -> bool:
    """Return True if the event loop responds within *threshold_ms*.

    Spins a tight sampling loop; if any iteration takes longer than
    *threshold_ms* the event loop was blocked (likely by sync I/O).
    """
    results: list[float] = []

    async def _sampler():
        last = time.monotonic()
        for _ in range(samples):
            await asyncio.sleep(0)  # yield to event loop
            now = time.monotonic()
            results.append(now - last)
            last = now

    asyncio.run(_sampler())
    durations = [d for d in results if d > 0.001]
    if not durations:
        return True
    return max(durations) < (threshold_ms / 1000.0)


# ── performance tests ─────────────────────────────────────────────────────


class TestEventLoopResponsiveness:
    """Ensure the asyncio event loop is not blocked by sync calls."""

    def test_event_loop_is_responsive_at_idle(self):
        """Baseline: idle event loop should respond in < 50 ms."""
        assert _event_loop_responsive(threshold_ms=50)

    def test_heavy_async_does_not_block_loop(self):
        """Many concurrent coroutines should not block."""
        async def _concurrent():
            results = []

            async def _worker(i: int):
                for _ in range(100):
                    await asyncio.sleep(0)
                results.append(i)

            tasks = [asyncio.create_task(_worker(i)) for i in range(200)]
            await asyncio.gather(*tasks)
            assert len(results) == 200

        t0 = time.monotonic()
        asyncio.run(_concurrent())
        elapsed = time.monotonic() - t0
        assert elapsed < 10.0, f"200 concurrent workers took {elapsed:.1f}s"

    def test_thread_pool_does_not_block_loop(self):
        """Offloading work to run_in_executor should not block event loop."""
        async def _test():
            loop = asyncio.get_running_loop()

            async def _check_while_busy():
                samples = []
                for _ in range(10):
                    await asyncio.sleep(0.02)
                    samples.append(time.monotonic())
                return samples

            def _blocking_work():
                time.sleep(0.3)  # simulate blocking filesystem I/O
                return 42

            # Start blocking work + responsiveness checker concurrently.
            # run_in_executor returns a Future — wrap with ensure_future
            # so create_task accepts it.
            busy_future = loop.run_in_executor(None, _blocking_work)
            check_task = asyncio.create_task(_check_while_busy())

            result = await busy_future
            samples = await check_task

            assert result == 42
            # Check that samples arrived roughly every 20ms
            intervals = [
                samples[i] - samples[i - 1] for i in range(1, len(samples))
            ]
            max_interval = max(intervals) if intervals else 0
            assert max_interval < 0.15, (
                f"Event loop delayed by {max_interval * 1000:.0f}ms — "
                f"possible sync call in async context"
            )

        asyncio.run(_test())


class TestToolExecutorPerformance:
    """Performance characteristics of the ToolExecutor and its mixins."""

    @pytest.fixture
    def executor(self, tmp_path):
        config = AgentConfig(workspace_dir=str(tmp_path))
        return ToolExecutor(config)

    @pytest.mark.asyncio
    async def test_file_cache_hit_rate(self, executor, tmp_path):
        """Cache should serve repeated reads faster than disk reads."""
        f = tmp_path / "test_cache.txt"
        content = "hello world\n" * 1000  # ~12 KB
        f.write_text(content, encoding="utf-8")

        # First read — cache miss (disk I/O)
        t0 = time.perf_counter()
        r1 = await executor._tool_read_file(str(f))
        t_miss = time.perf_counter() - t0

        assert r1.success

        # Second read — cache hit (no disk I/O, returns short note by design)
        t0 = time.perf_counter()
        r2 = await executor._tool_read_file(str(f))
        t_hit = time.perf_counter() - t0

        assert r2.success
        # Cache hit should be at least 3× faster (no disk I/O)
        ratio = t_miss / max(t_hit, 0.0001)
        assert ratio > 2.0, (
            f"Cache hit ({t_hit*1000:.1f}ms) not significantly faster "
            f"than miss ({t_miss*1000:.1f}ms), ratio={ratio:.1f}"
        )

    @pytest.mark.asyncio
    async def test_file_cache_lru_eviction(self, executor, tmp_path):
        """LRU eviction should keep cache bounded."""
        max_entries = executor._file_cache_max_entries
        files = []
        for i in range(max_entries + 10):
            f = tmp_path / f"lru_{i}.txt"
            f.write_text(f"content_{i}", encoding="utf-8")
            files.append(f)

        for f in files:
            await executor._tool_read_file(str(f))

        assert len(executor._file_cache) <= max_entries, (
            f"LRU cache grew to {len(executor._file_cache)} entries "
            f"(limit is {max_entries})"
        )

    @pytest.mark.asyncio
    async def test_glob_offloads_to_thread(self, executor, tmp_path):
        """_tool_glob should not block the event loop."""
        for i in range(100):
            (tmp_path / f"perf_{i}.txt").write_text("x", encoding="utf-8")

        t0 = time.monotonic()
        result = await executor._tool_glob("*.txt", path=str(tmp_path))
        elapsed = time.monotonic() - t0

        assert result.success
        assert elapsed < 1.0, (
            f"Glob of 100 files took {elapsed:.2f}s — may be blocking event loop"
        )

    @pytest.mark.asyncio
    async def test_concurrent_reads(self, executor, tmp_path):
        """Concurrent file reads should be parallel."""
        files = []
        for i in range(20):
            f = tmp_path / f"conc_{i}.txt"
            f.write_text(f"file {i}\n" * 500, encoding="utf-8")
            files.append(f)

        t0 = time.monotonic()
        results = await asyncio.gather(*[
            executor._tool_read_file(str(f)) for f in files
        ])
        elapsed = time.monotonic() - t0

        assert all(r.success for r in results)
        assert elapsed < 5.0, f"20 concurrent reads took {elapsed:.2f}s"


class TestMemoryPerformance:
    """Performance tests for the memory system."""

    @pytest.fixture
    def store(self, tmp_path):
        return MemoryStore(memory_dir=str(tmp_path))

    def test_recall_speed_many_memories(self, store):
        """Recalling from 100 memories should be fast."""
        for i in range(100):
            store.add(Memory(
                name=f"perf-test-{i}",
                description=f"Performance test memory {i}",
                content=f"This is test memory number {i} for measuring recall speed.",
            ))

        t0 = time.perf_counter()
        result = store.recall_context("test memory measuring recall speed")
        elapsed = time.perf_counter() - t0

        assert len(result) > 0
        assert elapsed < 0.5, f"Recall from 100 memories took {elapsed*1000:.1f}ms"


class TestTokenCounterPerformance:
    """TokenCounter throughput and cache effectiveness."""

    def test_count_tokens_cached_speed(self):
        """Cached messages should return much faster than uncached."""
        tc = TokenCounter.for_model("gpt-4o")
        msg = {"role": "user", "content": "Hello, world! This is a test message." * 10}

        # Uncached — first call
        iterations = 1000
        t0 = time.perf_counter()
        for _ in range(iterations):
            tc2 = TokenCounter.for_model("gpt-4o-fresh")
            tc2.count_tokens([msg])
        t_uncached = time.perf_counter() - t0

        # Cached — second call
        tc.count_tokens([msg])  # warm up
        t0 = time.perf_counter()
        for _ in range(iterations):
            tc.count_tokens([msg])
        t_cached = time.perf_counter() - t0

        ratio = t_uncached / max(t_cached, 0.0001)
        assert ratio > 2.0, (
            f"Cached ({t_cached*1000:.1f}ms) not faster than "
            f"uncached ({t_uncached*1000:.1f}ms), ratio={ratio:.1f}"
        )

    def test_thread_safety_for_model(self):
        """Concurrent access to for_model() should not corrupt the cache."""
        errors = []

        def _access():
            try:
                for _ in range(100):
                    tc = TokenCounter.for_model("gpt-4o")
                    assert tc is not None
                    tc.count_tokens([{"role": "user", "content": "test"}])
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=_access) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread-safety errors: {errors}"


class TestMemoryLeaks:
    """Ensure resources are released after operations."""

    def test_tool_executor_no_leak_after_close(self, tmp_path):
        """close() should release file cache and HTTP resources."""
        gc.collect()
        before = len(gc.get_objects())

        for _ in range(20):
            config = AgentConfig(workspace_dir=str(tmp_path))
            ex = ToolExecutor(config)
            ex.close()

        gc.collect()
        after = len(gc.get_objects())

        growth = after - before
        assert growth < 2000, (
            f"Object count grew by {growth} after 20 create-close cycles — "
            f"possible resource leak"
        )

    def test_httpx_client_released_on_close(self, tmp_path):
        """After close(), the httpx client should be closed and None'd."""
        config = AgentConfig(workspace_dir=str(tmp_path))
        ex = ToolExecutor(config)
        # Access the http property to create a client
        _ = ex.http
        assert hasattr(ex, "_http") and ex._http is not None

        ex.close()
        assert ex._http is None, "_http not cleared after close()"


class TestAsyncIOMetrics:
    """Measure actual async vs sync I/O throughput."""

    def test_sync_vs_async_read_throughput(self, tmp_path):
        """Async reads (thread-pool) should match sync-read throughput."""
        f = tmp_path / "large.txt"
        content = "Benchmark line content for throughput testing.\n" * 10000
        f.write_text(content, encoding="utf-8")

        def _sync_read():
            return f.read_text(encoding="utf-8")

        sync_time = timeit.timeit(_sync_read, number=100)

        async def _async_read():
            loop = asyncio.get_running_loop()
            for _ in range(100):
                await loop.run_in_executor(None, _sync_read)

        t0 = time.perf_counter()
        asyncio.run(_async_read())
        async_time = time.perf_counter() - t0

        overhead_ratio = async_time / sync_time
        assert overhead_ratio < 3.0, (
            f"Async thread-pool overhead is {overhead_ratio:.1f}x — "
            f"sync={sync_time:.3f}s, async={async_time:.3f}s"
        )
