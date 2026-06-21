"""
Shared type aliases for ATA Coder.

Single source of truth for Message, ToolDef, and other
commonly-referenced types that were previously duplicated
across llm_client.py, anthropic_client.py, and agent.py.

TypedDict variants are available for static analysis (mypy, pyright).
The legacy ``Message`` and ``ToolDef`` aliases remain for runtime
compatibility and minimal-change migrations.
"""

import random
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Callable, TypedDict

# ── Legacy aliases (widely used, keep for backward compat) ──────────────────

# OpenAI-compatible message dict: {"role": "...", "content": "...", ...}
Message = dict[str, Any]

# OpenAI-compatible tool definition dict
ToolDef = dict[str, Any]


# ── TypedDict variants (opt-in for stricter type checking) ──────────────────

class MessageDict(TypedDict, total=False):
    """OpenAI-compatible message. ``content`` may be None for tool_calls."""
    role: str
    content: str | None
    tool_calls: list[dict[str, Any]]
    tool_call_id: str
    name: str
    reasoning_content: str


class ToolDefDict(TypedDict, total=False):
    """OpenAI-compatible tool definition."""
    type: str
    function: dict[str, Any]


class ToolCallDict(TypedDict, total=False):
    """A single tool-call within an assistant message."""
    id: str
    type: str
    function: dict[str, Any]


class BaseLLMClient(ABC):
    """Abstract base for async LLM clients (OpenAI, Anthropic, etc.).

    Both LLMClient and AnthropicClient implement this interface so
    higher-level code (CoderAgent, SubAgent) can be written against
    the abstraction rather than branching on provider.

    All I/O methods are async — the client runs on the asyncio event loop.
    """

    @abstractmethod
    async def chat(self, messages: list[Message], tools: list[ToolDef] | None = None,
                   system_prompt: str = "") -> Message:
        """Send messages and return the assistant response."""
        ...

    @abstractmethod
    async def chat_stream(self, messages: list[Message], tools: list[ToolDef] | None = None,
                          system_prompt: str = "") -> AsyncIterator[tuple[str, Any]]:
        """Stream a chat completion. Yields (delta_type, content) tuples."""
        ...

    @abstractmethod
    def register_tools(self, tools: list[ToolDef]) -> None:
        """Register tool definitions for subsequent requests."""
        ...

    @abstractmethod
    def count_tokens_approx(self, messages: list[Message]) -> int:
        """Estimate token count for a message list."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Release resources (HTTP sessions, etc.)."""
        ...

    # ── Shared retry config (set by subclasses in __init__) ─────────────

    _max_retries: int = 3
    _retry_base_delay: float = 1.0  # seconds

    @staticmethod
    def _retry_delay(attempt: int, base_delay: float = 1.0,
                     retry_after: str = "") -> float:
        """Compute exponential backoff with jitter.  Capped at 60 s.

        Used by both ``LLMClient`` and ``AnthropicClient`` to eliminate
        the ~10 duplicated copies of the same backoff formula.

        When the server sends a ``retry-after`` header its value is
        used as the base; otherwise the delay doubles each attempt
        (1 s → 2 s → 4 s).  Uniform jitter (±50 %) is applied to
        spread out retry storms, then the result is clamped to 60 s.
        """
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = base_delay * (2 ** attempt)
        else:
            delay = base_delay * (2 ** attempt)
        delay *= (0.5 + random.random())  # jitter: spread concurrent retries
        return min(delay, 60.0)

    def on_usage(self, callback: Callable[[int, int], None]) -> None:
        """Register a callback for token usage: callback(prompt_tokens, completion_tokens).

        Default no-op — override if the client tracks usage.
        """
        pass
