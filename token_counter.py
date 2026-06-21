"""
Unified token counting service for ATA Coder.

Provides a single authoritative utility for estimating token counts
across all models, clients, and compaction paths.  Features:

- Model-aware encoding (cl100k_base for GPT-family, CJK fallback for others)
- LRU per-message caching (content-hash-keyed, stable across GC cycles)
- Batch counting with single tiktoken encode pass + fast cached-path
- Incremental `count_one()` for O(1) running-total tracking
- Singleton-per-model with bounded LRU eviction (max 16 models)

All other modules should import and use this instead of copy-pasting
CJK-aware heuristics or calling tiktoken directly.
"""

import logging
import re
import time
from collections import OrderedDict
from typing import Any

from .types import Message

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

# Regex for CJK / full-width characters that tiktoken doesn't handle well.
_CJK_RE = re.compile(r'[一-鿿　-〿＀-￯]')

# Simple token estimate: CJK ~1.5 char/token, Latin ~4 char/token.
_CJK_RATIO_NUM = 2
_CJK_RATIO_DEN = 3
_LATIN_RATIO_NUM = 1
_LATIN_RATIO_DEN = 4

# Models using o200k_base (GPT-4o family, o1/o3/o4 reasoning models)
_O200K_PREFIXES = ("gpt-4o", "gpt-4.1", "o1", "o3", "o4")
# cl100k_base fallback covers gpt-4, gpt-3.5, text-embedding, and non-OpenAI models

# Cache bound: max messages tracked per TokenCounter instance.
_MAX_CACHE_SIZE = 200
# Max per-model singleton instances before oldest is evicted.
_MAX_MODELS = 16


def _cjk_estimate(text: str) -> int:
    """Fast CJK-aware character-based token estimate for one string."""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return max(1, (cjk * _CJK_RATIO_NUM // _CJK_RATIO_DEN) + (other * _LATIN_RATIO_NUM // _LATIN_RATIO_DEN))


# ── TokenCounter ──────────────────────────────────────────────────────────────

class TokenCounter:
    """Unified token estimation with model-awareness and LRU message cache."""

    # Per-model singleton cache (keyed by model name), LRU-bounded
    _instances: "OrderedDict[str, TokenCounter]" = OrderedDict()

    def __init__(self, model: str = ""):
        self._model = model
        # LRU cache: hash(str(msg)) -> (timestamp, token_count)
        # Content-hash key avoids id()-reuse bugs after GC.
        self._cache: OrderedDict[int, tuple[float, int]] = OrderedDict()
        self._enc = None   # lazy-loaded tiktoken encoding
        self._has_tiktoken: bool | None = None

    # ── Singleton access ──────────────────────────────────────────────────

    @classmethod
    def for_model(cls, model: str) -> "TokenCounter":
        """Return (or create) a TokenCounter for *model*.

        Different models may use different tokenizers, so we cache per model.
        The instance cache is LRU-bounded to prevent unbounded growth when
        users switch models frequently.
        """
        key = model or "__default__"
        if key not in cls._instances:
            if len(cls._instances) >= _MAX_MODELS:
                # Evict least-recently-used model instance
                cls._instances.popitem(last=False)
            cls._instances[key] = cls(model)
        else:
            # Move to end for LRU tracking
            cls._instances.move_to_end(key)
        return cls._instances[key]

    # ── Encoding resolution ───────────────────────────────────────────────

    def _get_encoding(self):
        """Return a tiktoken Encoding for the current model, or None.

        Uses o200k_base for GPT-4o/4.1/o1/o3/o4 family, cl100k_base as fallback.
        Non-OpenAI models (Claude, DeepSeek, etc.) use cl100k_base — a reasonable
        approximation since tiktoken only ships OpenAI encodings.
        """
        if self._has_tiktoken is False:
            return None
        if self._enc is not None:
            return self._enc
        try:
            import tiktoken
        except ImportError:
            self._has_tiktoken = False
            return None

        self._has_tiktoken = True
        model_lower = self._model.lower()

        # o200k_base: GPT-4o family, o1/o3/o4 reasoning models
        if any(model_lower.startswith(p) for p in _O200K_PREFIXES):
            try:
                self._enc = tiktoken.get_encoding("o200k_base")
                return self._enc
            except Exception:
                pass  # fall through to cl100k_base fallback

        self._enc = tiktoken.get_encoding("cl100k_base")
        return self._enc

    # ── Public API ────────────────────────────────────────────────────────

    @staticmethod
    def _msg_key(msg: Message) -> int:
        """Stable content-based cache key (survives GC id reuse)."""
        return hash(str(msg))

    def count_one(self, msg: Message) -> int:
        """Count tokens for a single message. O(1) cache-hit path.

        This is the hot path for incremental token tracking — used by
        ContextManager.append() to maintain the running total.
        """
        key = self._msg_key(msg)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key][1]
        enc = self._get_encoding()
        ct = self._count_one(msg, enc)
        now = time.time()
        self._cache[key] = (now, ct)
        self._evict_lru()
        return ct

    def count_tokens(self, messages: list[Message]) -> int:
        """Return estimated token count for a list of messages."""
        return sum(self.batch_count(messages))

    def batch_count(self, messages: list[Message]) -> list[int]:
        """Return per-message token estimates.

        Fast path: if every message is already cached, return cached counts
        without any encoding. Otherwise falls through to uncached encoding.
        """
        # ── Fast path: all cached ─────────────────────────────────────────
        if messages and all(self._msg_key(m) in self._cache for m in messages):
            counts: list[int] = []
            for msg in messages:
                key = self._msg_key(msg)
                self._cache.move_to_end(key)
                counts.append(self._cache[key][1])
            return counts

        # ── Uncached path ─────────────────────────────────────────────────
        now = time.time()
        counts: list[int] = []
        uncached: list[tuple[int, Message]] = []

        for i, msg in enumerate(messages):
            key = self._msg_key(msg)
            if key in self._cache:
                self._cache.move_to_end(key)
                counts.append(self._cache[key][1])
            else:
                counts.append(0)  # placeholder
                uncached.append((i, msg))

        if uncached:
            enc = self._get_encoding()
            for idx, msg in uncached:
                ct = self._count_one(msg, enc)
                counts[idx] = ct
                key = self._msg_key(msg)
                self._cache[key] = (now, ct)

        self._evict_lru()
        return counts

    def estimate_text(self, text: str) -> int:
        """Estimate token count for a plain-text string (no caching)."""
        enc = self._get_encoding()
        if enc is not None:
            try:
                return len(enc.encode(text))
            except Exception:
                pass
        return _cjk_estimate(text)

    def clear_cache(self) -> None:
        """Clear the per-message token cache."""
        self._cache.clear()

    # ── Internals ─────────────────────────────────────────────────────────

    def _evict_lru(self) -> None:
        """Drop oldest entries when cache exceeds max size."""
        while len(self._cache) > _MAX_CACHE_SIZE:
            self._cache.popitem(last=False)

    def _count_one(self, msg: Message, enc) -> int:
        """Count tokens for a single message (encoding pass)."""
        if enc is not None:
            try:
                total = 0
                content = msg.get("content", "") or ""
                if isinstance(content, str):
                    total += len(enc.encode(content))
                elif isinstance(content, list):
                    # Anthropic content blocks
                    total += len(enc.encode(json_dumps(content)))
                for tc in msg.get("tool_calls", []):
                    total += len(enc.encode(json_dumps(tc)))
                return max(1, total)
            except Exception:
                pass

        # ── CJK-aware fallback ───────────────────────────────────────────
        content = msg.get("content", "") or ""
        if isinstance(content, list):
            content = json_dumps(content)
        total = _cjk_estimate(content)
        for tc in msg.get("tool_calls", []):
            total += _cjk_estimate(json_dumps(tc))
        return max(1, total)


# ── Module-level helpers ─────────────────────────────────────────────────────

import json as _json_mod  # noqa: E402

def json_dumps(obj: Any) -> str:
    """Fast JSON serialization — single source, avoids import per call."""
    return _json_mod.dumps(obj, sort_keys=True, ensure_ascii=False)


def get_token_counter(model: str = "") -> TokenCounter:
    """Shortcut to get a TokenCounter for *model*."""
    return TokenCounter.for_model(model)


def estimate_tokens(text: str, model: str = "") -> int:
    """Quick token estimate for a plain string."""
    return TokenCounter.for_model(model).estimate_text(text)


def count_messages(messages: list[Message], model: str = "") -> int:
    """Quick token count for a message list."""
    return TokenCounter.for_model(model).count_tokens(messages)
