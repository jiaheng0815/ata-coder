"""Tests for thread-safe EventQueue."""

import threading
from ata_coder.event_queue import EventQueue


class TestEventQueue:
    def test_put_and_get(self):
        eq = EventQueue()
        eq.put("hello")
        assert eq.get() == "hello"

    def test_get_empty_returns_none(self):
        eq = EventQueue()
        assert eq.get(timeout=0.1) is None

    def test_drain_all(self):
        eq = EventQueue()
        eq.put("a")
        eq.put("b")
        eq.put("c")
        events = eq.drain()
        assert events == ["a", "b", "c"]
        assert eq.count() == 0

    def test_drain_empty(self):
        eq = EventQueue()
        assert eq.drain() == []

    def test_count(self):
        eq = EventQueue()
        assert eq.count() == 0
        eq.put("x")
        eq.put("y")
        assert eq.count() == 2

    def test_clear(self):
        eq = EventQueue()
        eq.put("a")
        eq.put("b")
        eq.clear()
        assert eq.count() == 0

    def test_total_counters(self):
        eq = EventQueue()
        eq.put("a")
        eq.put("b")
        eq.drain()
        assert eq.total_put == 2
        assert eq.total_get == 2

    def test_thread_safety(self):
        eq = EventQueue()
        results = []
        errors = []

        def producer(n):
            try:
                for i in range(100):
                    eq.put(f"p{n}-{i}")
            except Exception as e:
                errors.append(e)

        def consumer():
            try:
                for _ in range(300):
                    ev = eq.get(timeout=0.5)
                    if ev:
                        results.append(ev)
                    if len(results) >= 300:
                        break
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=producer, args=(i,)) for i in range(3)
        ] + [threading.Thread(target=consumer)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors
        assert len(results) == 300
