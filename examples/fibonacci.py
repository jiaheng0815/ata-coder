"""
Fibonacci sequence — recursive and iterative implementations.

斐波那契数列的第 *n* 项定义：
    F(0) = 0, F(1) = 1
    F(n) = F(n-1) + F(n-2)   (n ≥ 2)

Usage:
    >>> from examples.fibonacci import fib_recursive, fib_iterative
    >>> fib_recursive(10)
    55
    >>> fib_iterative(10)
    55
"""

import functools


# ── helpers ──────────────────────────────────────────────────────────────

def _validate(n: int) -> int:
    """Validate that *n* is a non-negative integer.

    Raises:
        TypeError:  If *n* is not an integer.
        ValueError: If *n* is negative.
    """
    if not isinstance(n, int):
        raise TypeError(f"n must be an int, got {type(n).__name__}")
    if n < 0:
        raise ValueError(f"n must be ≥ 0, got {n}")
    return n


# ── recursive (memoized) ────────────────────────────────────────────────

@functools.lru_cache(maxsize=None)
def _fib_recursive(n: int) -> int:
    """Memoized recursive core — *n* is assumed to be a valid non-negative int."""
    if n < 2:
        return n
    return _fib_recursive(n - 1) + _fib_recursive(n - 2)


def fib_recursive(n: int) -> int:
    """Return the n-th Fibonacci number using **recursion + memoization**.

    Time complexity : O(n)
    Space complexity: O(n)  — call-stack depth + cache

    Notes
    -----
    A naive recursive version (without memoization) is O(2ⁿ) and will
    exhaust the stack for n ≈ 35+.  The ``@lru_cache`` decorator caches
    previously computed results, collapsing the tree to O(n).

    Parameters
    ----------
    n : int
        The index of the Fibonacci sequence (non-negative).

    Returns
    -------
    int
        The n-th Fibonacci number.

    Raises
    ------
    TypeError
        If *n* is not an integer.
    ValueError
        If *n* is negative.

    Examples
    --------
    >>> fib_recursive(0)
    0
    >>> fib_recursive(1)
    1
    >>> fib_recursive(10)
    55
    """
    _validate(n)
    return _fib_recursive(n)


def _fib_recursive_raw(n: int) -> int:
    """Naive recursive core — *n* is assumed to be a valid non-negative int."""
    if n < 2:
        return n
    return _fib_recursive_raw(n - 1) + _fib_recursive_raw(n - 2)


def fib_recursive_raw(n: int) -> int:
    """Return the n-th Fibonacci number using **naive recursion** (no cache).

    Time complexity : O(2ⁿ)  — **extremely slow** for n > 35
    Space complexity: O(n)   — call-stack depth

    This exists for **teaching purposes only**.  Use ``fib_recursive``
    (with memoization) or ``fib_iterative`` in real code.

    Parameters
    ----------
    n : int
        The index of the Fibonacci sequence (non-negative).

    Returns
    -------
    int
        The n-th Fibonacci number.

    Raises
    ------
    TypeError
        If *n* is not an integer.
    ValueError
        If *n* is negative.
    """
    _validate(n)
    return _fib_recursive_raw(n)


# ── iterative ───────────────────────────────────────────────────────────

def fib_iterative(n: int) -> int:
    """Return the n-th Fibonacci number using **iteration** (bottom-up DP).

    Time complexity : O(n)
    Space complexity: O(1)

    This is the most practical implementation for any *n* that fits in
    memory (up to n ≈ 10⁶ without overflow — Python big‑ints handle the
    rest).

    Parameters
    ----------
    n : int
        The index of the Fibonacci sequence (non-negative).

    Returns
    -------
    int
        The n-th Fibonacci number.

    Raises
    ------
    TypeError
        If *n* is not an integer.
    ValueError
        If *n* is negative.

    Examples
    --------
    >>> fib_iterative(0)
    0
    >>> fib_iterative(1)
    1
    >>> fib_iterative(10)
    55
    >>> fib_iterative(100)  # 大数依然正确
    354224848179261915075
    """
    _validate(n)
    if n < 2:
        return n

    a, b = 0, 1
    for _ in range(2, n + 1):
        a, b = b, a + b
    return b


# ── convenience ──────────────────────────────────────────────────────────

def fib(n: int, method: str = "iterative") -> int:
    """Unified interface for Fibonacci computation.

    Parameters
    ----------
    n : int
        The index of the Fibonacci sequence (non-negative).
    method : {"iterative", "recursive", "raw"}
        Which algorithm to use.

    Returns
    -------
    int
        The n-th Fibonacci number.

    Raises
    ------
    ValueError
        If *method* is not recognized.
    """
    if method == "iterative":
        return fib_iterative(n)
    elif method == "recursive":
        return fib_recursive(n)
    elif method == "raw":
        return fib_recursive_raw(n)
    else:
        raise ValueError(f"Unknown method: {method!r}")


# ── CLI quick‑test ───────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        try:
            n = int(sys.argv[1])
        except (ValueError, IndexError):
            print(f"Error: {sys.argv[1]!r} is not a valid integer.", file=sys.stderr)
            print("Usage: python -m examples.fibonacci [n]", file=sys.stderr)
            sys.exit(1)
    else:
        n = 10

    print(f"fib_iterative({n}) = {fib_iterative(n)}")
    print(f"fib_recursive({n}) = {fib_recursive(n)}")

    if n <= 35:
        print(f"fib_recursive_raw({n}) = {fib_recursive_raw(n)}")
    else:
        print("fib_recursive_raw: skipped (n > 35 would be too slow)")
