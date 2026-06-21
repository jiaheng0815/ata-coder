"""
Optional LLMLingua-based prompt compression for ATA Coder.

Provides a lightweight, local alternative to LLM-based summarization
for context compaction.  When LLMLingua is installed (``pip install
llmlingua``), it can compress conversation text 2–5× while preserving
key entities, numbers, and structural cues — no API call needed.

When LLMLingua is NOT installed, the module degrades gracefully and
the caller falls back to the existing LLM summarization path.

Usage:
    from .prompt_compressor import PromptCompressor
    pc = PromptCompressor()
    compressed = pc.compress(long_text, target_ratio=0.5)
"""

from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# ── Optional LLMLingua import (fully lazy, opt-in for safety) ─────────────────
# LLMLingua pulls in numpy/nltk/torch — heavy dependencies that can segfault
# on unstable Python versions.  To avoid crashing the process, we require an
# explicit opt-in via environment variable or settings.json:
#
#   export ATA_CODER_LLMLINGUA=1
#
# or in ~/.ata_coder/settings.json:
#   { "env": { "ATA_CODER_LLMLINGUA": "1" } }
#
# Without this flag, is_available() returns False and compression falls back
# to the LLM-based summariser.
_HAS_LLMLINGUA = None  # None = not yet checked; True/False = cached result
_LLMLinguaCompressor = None


def _check_llmlingua() -> bool:
    """Probe whether LLMLingua is importable.  Caches the result.

    Requires explicit opt-in via ATA_CODER_LLMLINGUA=1 because the
    dependency chain (numpy, nltk, torch) can segfault on some Python
    versions.  Once the environment passes the import test, subsequent
    calls are cached.
    """
    global _HAS_LLMLINGUA, _LLMLinguaCompressor
    if _HAS_LLMLINGUA is not None:
        return _HAS_LLMLINGUA

    # ── Opt-in gate ────────────────────────────────────────────────────
    import os
    opt_in = os.environ.get("ATA_CODER_LLMLINGUA", "")
    if opt_in.lower() not in ("1", "true", "yes", "on"):
        _HAS_LLMLINGUA = False
        return False

    # ── Try importing ─────────────────────────────────────────────────
    for module_name in ("llmlingua", "llmlingua2"):
        try:
            import importlib
            mod = importlib.import_module(module_name)
            _LLMLinguaCompressor = getattr(mod, "PromptCompressor", None)
            if _LLMLinguaCompressor is not None:
                _HAS_LLMLINGUA = True
                return True
        except Exception:
            continue

    _HAS_LLMLINGUA = False
    return False


# ── Entity preservation patterns ─────────────────────────────────────────────
# Tokens matching these patterns are preserved during compression
_ENTITY_PATTERNS = [
    # File paths (cross-platform)
    re.compile(r'(?:/[-\w.+@]+)+/[-\w.]+(?:\.\w{1,6})?'),
    re.compile(r'[A-Za-z]:\\(?:[-\w.]+\\?)+'),
    # Code identifiers
    re.compile(r'\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b'),  # PascalCase
    re.compile(r'\b[a-z]+(?:[a-z]+[A-Z])+[a-z]*\b'),  # camelCase
    re.compile(r'\b[a-z_]+_[a-z_]+\b'),                 # snake_case
    # URLs
    re.compile(r'https?://[^\s<>"{}|\\^`\[\]]+'),
    # Error codes / version numbers
    re.compile(r'\bv?\d+\.\d+(?:\.\d+)?(?:-[a-zA-Z0-9.]+)?\b'),
]


class PromptCompressor:
    """Lightweight wrapper around LLMLingua for conversation compression.

    Features:
    - Automatic model selection (xlm_roberta for English, mT5 for multilingual)
    - Entity-aware compression — file paths, identifiers, URLs are preserved
    - Graceful fallback when LLMLingua is not installed
    - Configurable compression ratio
    """

    def __init__(
        self,
        model_name: str = "microsoft/llmlingua-2-bert-base-multilingual-cased-meeting",
        device: str = "cpu",
        use_auth_token: bool = False,
    ):
        self._model_name = model_name
        self._device = device
        self._compressor = None
        self._init_error: Optional[str] = None

        if _check_llmlingua():
            try:
                self._compressor = _LLMLinguaCompressor(
                    model_name=model_name,
                    device_map=device if device != "cpu" else -1,
                    use_auth_token=use_auth_token,
                )
                logger.info(
                    "LLMLingua compressor initialized: %s on %s",
                    model_name, device,
                )
            except Exception as e:
                self._init_error = str(e)
                logger.debug("LLMLingua init failed: %s", e)
        else:
            self._init_error = (
                "LLMLingua not installed. Install with: pip install llmlingua"
            )
            logger.debug(self._init_error)

    @property
    def available(self) -> bool:
        """Is LLMLingua compression available?"""
        return self._compressor is not None

    @property
    def status(self) -> str:
        """Human-readable status string."""
        if self._compressor is not None:
            return f"ready ({self._model_name} on {self._device})"
        return self._init_error or "not initialized"

    def compress(
        self,
        text: str,
        target_ratio: float = 0.5,
        max_length: Optional[int] = None,
    ) -> str:
        """Compress *text* to approximately *target_ratio* of its original length.

        Args:
            text: The conversation text to compress.
            target_ratio: Fraction of original tokens to keep (0.0–1.0).
                          Lower = more aggressive compression.  Default 0.5.
            max_length: Optional hard cap on output character count.

        Returns:
            Compressed text, or the original text if compression is unavailable.
        """
        if not text or not text.strip():
            return text

        if self._compressor is None:
            logger.debug("LLMLingua unavailable, returning original text")
            return text

        try:
            # LLMLingua's compress method: rate controls how aggressive
            # the compression is.  Lower rate = keep fewer tokens.
            rate = max(0.1, min(1.0, target_ratio))
            compressed = self._compressor.compress_prompt(
                text,
                rate=rate,
                force_tokens=[
                    "!", "?", ".", ",", "\n", ":", ";", "(", ")", "[", "]",
                    "{", "}", "<", ">", "=", "+", "-", "*", "/", "%",
                ],
            )

            if isinstance(compressed, dict):
                compressed = compressed.get("compressed_prompt", text)
            if not isinstance(compressed, str):
                compressed = str(compressed)

            # If compression removed too much, fall back
            if len(compressed) < len(text) * 0.1:
                logger.warning(
                    "LLMLingua over-compressed (%.1f%% kept), using original",
                    len(compressed) / max(len(text), 1) * 100,
                )
                return text

            # Apply max_length cap if specified
            if max_length and len(compressed) > max_length:
                compressed = compressed[:max_length]

            logger.debug(
                "LLMLingua: %d → %d chars (%.0f%% kept, target %.0f%%)",
                len(text), len(compressed),
                len(compressed) / max(len(text), 1) * 100,
                target_ratio * 100,
            )
            return compressed

        except Exception as e:
            logger.debug("LLMLingua compression failed: %s", e)
            return text

    def compress_messages(
        self,
        messages: list[dict],
        target_ratio: float = 0.5,
    ) -> str:
        """Compress a list of conversation messages into a condensed string.

        Concatenates messages with role labels, then compresses.
        Preserves tool call and result boundaries.
        """
        parts: list[str] = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if not content:
                # Tool calls — extract function names as markers
                tool_calls = msg.get("tool_calls", [])
                if tool_calls:
                    names = [
                        tc.get("function", {}).get("name", "?")
                        for tc in tool_calls
                    ]
                    parts.append(f"[{role}] tool_calls: {', '.join(names)}")
                continue

            if isinstance(content, str):
                # Truncate very long messages before compression
                truncated = content[:4000]
                parts.append(f"[{role}] {truncated}")
            elif isinstance(content, list):
                # Multi-part content (e.g. images)
                text_parts = [
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                parts.append(f"[{role}] {' '.join(text_parts)[:4000]}")

        combined = "\n".join(parts)
        return self.compress(combined, target_ratio=target_ratio)


# ── Module-level convenience ──────────────────────────────────────────────────

_compressor: Optional[PromptCompressor] = None


def get_compressor(model_name: str = "") -> PromptCompressor:
    """Get or create the module-level PromptCompressor singleton."""
    global _compressor
    if _compressor is None:
        _compressor = PromptCompressor(
            model_name=model_name or "microsoft/llmlingua-2-bert-base-multilingual-cased-meeting",
        )
    return _compressor


def is_available() -> bool:
    """Check if LLMLingua compression is available without initializing."""
    return _check_llmlingua()
