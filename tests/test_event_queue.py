"""Tests for async EventQueue."""

import asyncio
import pytest
from ata_coder.event_queue import EventQueue


class TestEventQueue:
    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_put_and_get(self):
        eq = EventQueue()
        await eq.put("hello")
        assert await eq.get() == "hello"

    @pytest.mark.asyncio
    async def test_get_with_timeout_empty(self):
        eq = EventQueue()
        assert await eq.get(timeout=0.05) is None

    @pytest.mark.asyncio
    async def test_get_with_timeout_populated(self):
        eq = EventQueue()
        await eq.put("x")
        assert await eq.get(timeout=0.1) == "x"

    @pytest.mark.asyncio
    async def test_drain_all(self):
        eq = EventQueue()
        await eq.put("a")
        await eq.put("b")
        await eq.put("c")
        events = await eq.drain()
        assert events == ["a", "b", "c"]
        assert eq.count() == 0

    @pytest.mark.asyncio
    async def test_drain_empty(self):
        eq = EventQueue()
        assert await eq.drain() == []

    @pytest.mark.asyncio
    async def test_count(self):
        eq = EventQueue()
        assert eq.count() == 0
        await eq.put("x")
        await eq.put("y")
        assert eq.count() == 2

    @pytest.mark.asyncio
    async def test_clear(self):
        eq = EventQueue()
        await eq.put("a")
        await eq.put("b")
        await eq.clear()
        assert eq.count() == 0

    @pytest.mark.asyncio
    async def test_total_counters(self):
        eq = EventQueue()
        await eq.put("a")
        await eq.put("b")
        await eq.drain()
        assert eq.total_put == 2
        assert eq.total_get == 2

    @pytest.mark.asyncio
    async def test_put_nowait(self):
        eq = EventQueue()
        eq.put_nowait("instant")
        assert eq.count() == 1
        assert await eq.get() == "instant"

    @pytest.mark.asyncio
    async def test_concurrent_producers_consumers(self):
        """Multiple asyncio tasks producing and consuming concurrently."""
        eq = EventQueue(maxsize=100)
        results: list[str] = []
        n_producers = 3
        n_per_producer = 50

        async def producer(n: int):
            for i in range(n_per_producer):
                await eq.put(f"p{n}-{i}")

        async def consumer():
            collected = 0
            total = n_producers * n_per_producer
            while collected < total:
                ev = await eq.get(timeout=0.5)
                if ev:
                    results.append(ev)
                    collected += 1

        producers = [asyncio.create_task(producer(i)) for i in range(n_producers)]
        cons = asyncio.create_task(consumer())

        await asyncio.gather(*producers)
        await asyncio.wait_for(cons, timeout=10)

        assert len(results) == n_producers * n_per_producer
