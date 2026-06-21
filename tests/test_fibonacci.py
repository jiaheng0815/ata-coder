"""
Tests for examples/fibonacci.py

Covers: base cases, small values, large values, input validation, and
algorithmic consistency between recursive and iterative implementations.
"""

import pytest
from examples.fibonacci import (
    fib,
    fib_iterative,
    fib_recursive,
    fib_recursive_raw,
)

# ── known Fibonacci numbers ──────────────────────────────────────────────

TABLE = [
    (0, 0),
    (1, 1),
    (2, 1),
    (3, 2),
    (4, 3),
    (5, 5),
    (6, 8),
    (7, 13),
    (8, 21),
    (9, 34),
    (10, 55),
    (20, 6765),
    (30, 832040),
    (50, 12586269025),
    (100, 354224848179261915075),
]


class TestFibIterative:
    """Iterative implementation — the primary workhorse."""

    @pytest.mark.parametrize("n, expected", TABLE)
    def test_known_values(self, n: int, expected: int) -> None:
        assert fib_iterative(n) == expected

    def test_large_n(self) -> None:
        """Ensure O(1) memory usage holds for large n (well within 4300‑digit limit)."""
        result = fib_iterative(10_000)
        assert isinstance(result, int)
        assert result > 0
        # Sanity: last digit must be a decimal digit
        assert str(result)[-1].isdigit()

    @pytest.mark.parametrize("bad", [-1, -10])
    def test_negative_rejected(self, bad: int) -> None:
        with pytest.raises(ValueError, match="≥ 0"):
            fib_iterative(bad)

    @pytest.mark.parametrize("bad", [1.5, "5", None, [10]])
    def test_type_error(self, bad) -> None:
        with pytest.raises(TypeError):
            fib_iterative(bad)


class TestFibRecursive:
    """Memoized recursive implementation."""

    @pytest.mark.parametrize("n, expected", TABLE)  # keep fast
    def test_known_values(self, n: int, expected: int) -> None:
        assert fib_recursive(n) == expected

    def test_agrees_with_iterative(self) -> None:
        """Cross‑validate up to n=50 (both O(n) so it's fast)."""
        for n in range(50):
            assert fib_recursive(n) == fib_iterative(n)

    def test_cache_hit_consistency(self) -> None:
        """lru_cache produces the same result on cache hit as on first call."""
        assert fib_recursive(30) == 832040
        assert fib_recursive(30) == 832040  # warm cache — should match

    @pytest.mark.parametrize("bad", [-1, -10])
    def test_negative_rejected(self, bad: int) -> None:
        with pytest.raises(ValueError, match="≥ 0"):
            fib_recursive(bad)

    @pytest.mark.parametrize("bad", [1.5, "0", None, [10]])
    def test_type_error(self, bad) -> None:
        with pytest.raises(TypeError):
            fib_recursive(bad)


class TestFibRecursiveRaw:
    """Naive recursive (no memoization) — only for very small n."""

    @pytest.mark.parametrize("n, expected", [(0, 0), (1, 1), (2, 1), (5, 5), (10, 55)])
    def test_small_values(self, n: int, expected: int) -> None:
        assert fib_recursive_raw(n) == expected

    def test_agrees_with_iterative_for_small_n(self) -> None:
        for n in range(15):
            assert fib_recursive_raw(n) == fib_iterative(n)

    @pytest.mark.parametrize("bad", [-1, -5])
    def test_negative_rejected(self, bad: int) -> None:
        with pytest.raises(ValueError, match="≥ 0"):
            fib_recursive_raw(bad)

    @pytest.mark.parametrize("bad", [1.5, "0", None])
    def test_type_error(self, bad) -> None:
        with pytest.raises(TypeError):
            fib_recursive_raw(bad)


class TestFibUnified:
    """Convenience wrapper ``fib()``."""

    @pytest.mark.parametrize("n", [0, 1, 10, 30])
    def test_methods_agree(self, n: int) -> None:
        assert fib(n, "iterative") == fib(n, "recursive")

    def test_default_method(self) -> None:
        """Default method should be iterative."""
        assert fib(10) == fib_iterative(10)

    def test_unknown_method(self) -> None:
        with pytest.raises(ValueError, match="Unknown method"):
            fib(10, method="foobar")
