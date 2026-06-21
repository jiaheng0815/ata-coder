"""Sliding-window rate limiter for the HTTP API server.

Extracted from server.py to reduce file size and isolate concerns.
Used by AgentAPIHandler as a class-level mixin.
"""

import collections
import logging
import threading
import time

logger = logging.getLogger(__name__)


class RateLimiter:
    """Sliding-window rate limiter with deque for O(1) expiry + penalty tier.

    Usage (as class-level mixin on a BaseHTTPRequestHandler subclass):
        class MyHandler(RateLimiter, BaseHTTPRequestHandler):
            ...
            allowed = self._check_rate_limit(self.client_address[0])
    """

    # ── Configuration ──────────────────────────────────────────────────────
    _rate_lock: threading.Lock = threading.Lock()
    _rate_buckets: dict[str, "collections.deque[float]"] = {}  # ip → deque of timestamps
    _rate_blocked: dict[str, float] = {}  # ip → block expiry timestamp
    _RATE_MAX_REQUESTS = 120   # max requests per window
    _RATE_WINDOW_S = 60.0      # sliding window in seconds
    _RATE_BLOCK_S = 300.0      # block duration after exceeding penalty threshold
    _RATE_PENALTY_MULTIPLIER = 3  # requests × this = block threshold
    _RATE_CLEANUP_INTERVAL = 1000  # amortized: trigger cleanup every N calls
    _rate_cleanup_counter: int = 0

    @classmethod
    def _cleanup_rate_buckets(cls, now: float) -> None:
        """Remove stale IP entries whose last activity exceeds 2× the window."""
        cutoff = now - cls._RATE_WINDOW_S * 2
        stale = [
            ip for ip, dq in cls._rate_buckets.items()
            if not dq or dq[-1] <= cutoff
        ]
        for ip in stale:
            del cls._rate_buckets[ip]
        if stale:
            logger.debug("Rate limiter: pruned %d stale IP bucket(s)", len(stale))
        # Also clean up expired blocked IPs (never unblocked if they don't return)
        expired_blocks = [
            ip for ip, until in cls._rate_blocked.items() if now >= until
        ]
        for ip in expired_blocks:
            del cls._rate_blocked[ip]
        if expired_blocks:
            logger.debug("Rate limiter: unblocked %d IP(s)", len(expired_blocks))

    @classmethod
    def _check_rate_limit(cls, client_ip: str) -> bool:
        """Sliding-window rate limiter. Returns True if request is allowed."""
        now = time.time()
        with cls._rate_lock:
            # Periodic stale-bucket cleanup (amortized)
            cls._rate_cleanup_counter += 1
            if cls._rate_cleanup_counter >= cls._RATE_CLEANUP_INTERVAL:
                cls._rate_cleanup_counter = 0
                cls._cleanup_rate_buckets(now)

            # Check if IP is currently blocked (penalty tier)
            blocked_until = cls._rate_blocked.get(client_ip, 0)
            if now < blocked_until:
                return False
            if now >= blocked_until and client_ip in cls._rate_blocked:
                del cls._rate_blocked[client_ip]

            dq = cls._rate_buckets.get(client_ip)
            if dq is None:
                dq = collections.deque()
                cls._rate_buckets[client_ip] = dq

            # Purge expired entries — O(1) per entry via popleft
            cutoff = now - cls._RATE_WINDOW_S
            while dq and dq[0] <= cutoff:
                dq.popleft()

            # Penalty tier: block if request count exceeds penalty threshold
            penalty_limit = cls._RATE_MAX_REQUESTS * cls._RATE_PENALTY_MULTIPLIER
            if len(dq) > penalty_limit:
                cls._rate_blocked[client_ip] = now + cls._RATE_BLOCK_S
                logger.warning("Rate limit BLOCK: %s for %ds (%d requests in window)",
                               client_ip, cls._RATE_BLOCK_S, len(dq))
                return False

            # Standard rate limit
            if len(dq) >= cls._RATE_MAX_REQUESTS:
                return False

            dq.append(now)
        return True
