"""
OpenAI-compatible async LLM client with tool/function calling support.
Uses httpx.AsyncClient (no openai SDK dependency) for maximum compatibility.
Supports any provider that implements the OpenAI chat completions API format.
"""

import asyncio
import json
import logging
import random
from typing import Any, AsyncIterator, Callable

import httpx

from .config import LLMConfig
from .types import BaseLLMClient, Message, ToolDef
from .utils import enhance_api_error

logger = logging.getLogger(__name__)


# ── System prompt for the coding agent ───────────────────────────────────────

_SYSTEM_PROMPT_CACHE: str | None = None


def _load_system_prompt() -> str:
    """Load fallback system prompt from skills/codecraft/SKILL.md if available.

    Cached after first call — no file I/O on repeated access.
    """
    global _SYSTEM_PROMPT_CACHE
    if _SYSTEM_PROMPT_CACHE is not None:
        return _SYSTEM_PROMPT_CACHE

    import re
    from pathlib import Path
    prompt_file = Path(__file__).parent / "skills" / "codecraft" / "SKILL.md"
    if prompt_file.exists():
        try:
            raw = prompt_file.read_text(encoding="utf-8")
            match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", raw, re.DOTALL)
            if match:
                _SYSTEM_PROMPT_CACHE = match.group(2).strip()
                return _SYSTEM_PROMPT_CACHE
            _SYSTEM_PROMPT_CACHE = raw
            return raw
        except (OSError, UnicodeDecodeError) as e:
            logger = logging.getLogger(__name__)
            logger.warning("Failed to load system prompt from %s: %s", prompt_file, e)
    _SYSTEM_PROMPT_CACHE = (
        "You are an expert software engineer embedded in a coding agent. "
        "Understand intent, navigate the codebase, make precise surgical edits, "
        "verify changes, and communicate clearly. "
        "ONE problem per change. NEVER refactor alongside a bugfix. "
        "Read before edit — never guess file contents. "
        "Match existing code style. Report outcomes faithfully."
    )
    return _SYSTEM_PROMPT_CACHE


# Lazy alias — defers loading until first access so that the skills directory
# is guaranteed to exist.  Module-level ``__getattr__`` (Python 3.7+) only
# fires when the attribute is NOT found in the module dict.
_SYSTEM_PROMPT_LAZY: str | None = None


def __getattr__(name: str):
    if name == "SYSTEM_PROMPT":
        global _SYSTEM_PROMPT_LAZY
        if _SYSTEM_PROMPT_LAZY is None:
            _SYSTEM_PROMPT_LAZY = _load_system_prompt()
        return _SYSTEM_PROMPT_LAZY
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class LLMClient(BaseLLMClient):
    """
    OpenAI-compatible async LLM client using httpx.AsyncClient.

    Supports:
    - Any OpenAI-compatible endpoint (OpenAI, Azure, Ollama, vLLM, etc.)
    - Function/tool calling
    - Streaming and non-streaming modes
    - Rate limit retry with exponential backoff
    - Usage tracking callback
    """

    def __init__(self, config: LLMConfig | None = None):
        self.config = config or LLMConfig()
        self._tools: list[ToolDef] = []

        # Build the HTTP client — URL normalization via shared module
        from .model_registry import build_api_url
        self._api_url = build_api_url(self.config.base_url, "chat/completions")

        self._headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=30.0),
            headers=self._headers,
        )

        # Usage tracking
        self._usage_callback: Callable[[int, int], None] | None = None
        self._total_prompt_tokens = 0
        self._total_completion_tokens = 0
        # The most recent exact prompt-token count from the API (None until
        # the first response).  Used by the agent to cross-check local estimates.
        self.last_exact_prompt_tokens: int | None = None

        # Retry config
        self._max_retries = 3
        self._retry_base_delay = 1.0  # seconds

    def on_usage(self, callback: Callable[[int, int], None]) -> None:
        """Register a callback for token usage: callback(prompt_tokens, completion_tokens)."""
        self._usage_callback = callback

    @property
    def total_prompt_tokens(self) -> int:
        return self._total_prompt_tokens

    @property
    def total_completion_tokens(self) -> int:
        return self._total_completion_tokens

    @property
    def total_tokens(self) -> int:
        return self._total_prompt_tokens + self._total_completion_tokens

    # ── Tool registration ──────────────────────────────────────────────────

    def register_tools(self, tools: list[ToolDef]) -> None:
        """Register tool definitions for subsequent requests."""
        self._tools = tools

    # ── Chat completion (non-streaming) ────────────────────────────────────

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
        system_prompt: str = "",
    ) -> Message:
        """
        Send messages and get a completion.
        Returns the assistant message (may include tool_calls).
        Automatically retries on rate limit (429) errors.

        *system_prompt* is prepended as a system message when the messages
        list does not already contain one.  This provides API parity with
        AnthropicClient without requiring the caller to branch on provider.
        """
        tool_defs = tools if tools is not None else self._tools

        # Honour system_prompt param for API parity with AnthropicClient
        resolved_messages = list(messages)
        if system_prompt and not any(m.get("role") == "system" for m in resolved_messages):
            resolved_messages.insert(0, {"role": "system", "content": system_prompt})

        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": resolved_messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }

        if tool_defs:
            body["tools"] = tool_defs
            body["tool_choice"] = "auto"

        # Thinking mode
        thinking_strength = getattr(self.config, 'thinking_strength', '') or ''
        if thinking_strength == "off":
            body["extra_body"] = {"thinking": {"type": "disabled"}}
        elif thinking_strength:
            body["reasoning_effort"] = thinking_strength.lower()
            body.pop("temperature", None)

        logger.debug(
            "Calling %s with %d messages, %d tools, thinking=%s",
            self.config.model,
            len(messages),
            len(tool_defs) if tool_defs else 0,
            thinking_strength or "off",
        )

        # Sanitize surrogates before JSON encoding (prevent UTF-8 encode crash)
        from .utils import sanitize_surrogates
        body = sanitize_surrogates(body)

        data = await self._request_with_retry(body)

        choice = data["choices"][0]
        msg = choice["message"]

        # Build a clean message dict (preserve reasoning_content for DeepSeek v4/etc)
        result: Message = {
            "role": "assistant",
            "content": msg.get("content") or "",
        }

        # Preserve reasoning_content for thinking/reasoning models (DeepSeek R1/v4, etc.)
        if msg.get("reasoning_content"):
            result["reasoning_content"] = msg["reasoning_content"]

        if msg.get("tool_calls"):
            result["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": tc["function"]["arguments"],
                    },
                }
                for tc in msg["tool_calls"]
            ]

        # Track usage (with fallback estimation)
        usage = data.get("usage")
        if usage and usage.get("total_tokens"):
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            self.last_exact_prompt_tokens = prompt_tokens
        else:
            # Fallback estimation
            prompt_tokens = self.count_tokens_approx(messages)
            completion_tokens = self.count_tokens_approx([result])
        self._total_prompt_tokens += prompt_tokens
        self._total_completion_tokens += completion_tokens
        logger.info(
            "Tokens: %d in, %d out (session: %d total)",
            prompt_tokens, completion_tokens, self.total_tokens,
        )
        if self._usage_callback:
            self._usage_callback(prompt_tokens, completion_tokens)

        return result

    # ── Streaming chat completion ──────────────────────────────────────────

    async def chat_stream(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
        system_prompt: str = "",
    ) -> AsyncIterator[tuple[str, Any]]:
        """
        Stream a chat completion.
        Yields (delta_type, content) tuples.
        delta_type is one of: "text", "tool_call", "finish".

        *system_prompt* is prepended as a system message when the messages
        list does not already contain one — matching the behaviour of chat().
        """
        tool_defs = tools if tools is not None else self._tools

        # Honour system_prompt param for API parity with AnthropicClient
        resolved_messages = list(messages)
        if system_prompt and not any(m.get("role") == "system" for m in resolved_messages):
            resolved_messages.insert(0, {"role": "system", "content": system_prompt})

        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": resolved_messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": True,
        }

        if tool_defs:
            body["tools"] = tool_defs
            body["tool_choice"] = "auto"

        # Thinking mode for streaming
        thinking_strength = getattr(self.config, 'thinking_strength', '') or ''
        if thinking_strength == "off":
            body["extra_body"] = {"thinking": {"type": "disabled"}}
        elif thinking_strength:
            body["reasoning_effort"] = thinking_strength.lower()
            body.pop("temperature", None)

        # Retry loop for streaming (up to 2 retries for 429/5xx)

        # Sanitize surrogates before JSON encoding (prevent UTF-8 encode crash)
        from .utils import sanitize_surrogates
        body = sanitize_surrogates(body)

        for attempt in range(self._max_retries):
            try:
                response = await self._client.send(
                    self._client.build_request("POST", self._api_url, json=body),
                    stream=True,
                )
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
                if attempt < self._max_retries - 1:
                    delay = self._retry_delay(attempt, self._retry_base_delay)
                    logger.warning("Stream connect error, retrying in %.1fs: %s", delay, e)
                    await asyncio.sleep(delay)
                    continue
                raise RuntimeError(f"Stream connection failed: {e}") from e

            if response.status_code >= 400:
                if response.status_code == 429 and attempt < self._max_retries - 1:
                    delay = self._retry_delay(attempt, self._retry_base_delay)
                    logger.warning("Stream rate limited, retrying in %.1fs", delay)
                    await asyncio.sleep(delay)
                    continue
                if response.status_code >= 500 and attempt < self._max_retries - 1:
                    delay = self._retry_delay(attempt, self._retry_base_delay)
                    logger.warning("Stream server error (%d), retrying in %.1fs", response.status_code, delay)
                    await asyncio.sleep(delay)
                    continue
                try:
                    error_body = (await response.aread()).decode("utf-8", errors="replace")[:500]
                except Exception:
                    error_body = "(could not read body)"
                logger.error("Stream request failed (%d): %s", response.status_code, error_body)
                response.raise_for_status()

            # Success — read the streaming response body

            # Accumulators for streaming tool calls
            tool_call_buf: dict[int, dict[str, Any]] = {}
            finish_reason = None

            # Track usage from streaming (with fallback estimation)
            prompt_tokens = 0
            completion_tokens = 0
            total_text_chars = 0  # fallback estimation

            async for line in response.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue

                data_str = line[6:]  # strip "data: " prefix
                if data_str.strip() == "[DONE]":
                    break

                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    logger.warning("Failed to parse SSE line: %s", data_str[:100])
                    continue

                # Track usage if present in chunk
                usage = chunk.get("usage")
                if usage:
                    prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
                    completion_tokens = usage.get("completion_tokens", completion_tokens)

                choices = chunk.get("choices", [])
                if not choices:
                    continue

                choice = choices[0]
                delta = choice.get("delta", {})
                finish_reason = choice.get("finish_reason") or finish_reason

                # Text content
                if delta.get("content"):
                    text_chunk = delta["content"]
                    total_text_chars += len(text_chunk)
                    yield ("text", text_chunk)

                # Reasoning content (DeepSeek R1/v4, o1 models)
                if delta.get("reasoning_content"):
                    yield ("reasoning", delta["reasoning_content"])

                # Tool calls (streaming — incremental chunks)
                tool_calls_delta = delta.get("tool_calls", [])
                for tc_delta in tool_calls_delta:
                    idx = tc_delta.get("index", 0)
                    if idx not in tool_call_buf:
                        tool_call_buf[idx] = {
                            "id": "",
                            "function": {"name": "", "arguments": ""},
                        }
                    buf = tool_call_buf[idx]
                    if tc_delta.get("id"):
                        buf["id"] = tc_delta["id"]
                    fn = tc_delta.get("function", {})
                    if fn.get("name"):
                        buf["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        buf["function"]["arguments"] += fn["arguments"]

            # Yield assembled tool calls
            for idx in sorted(tool_call_buf.keys()):
                buf = tool_call_buf[idx]
                yield ("tool_call", {
                    "id": buf["id"],
                    "type": "function",
                    "function": buf["function"],
                })

            # Track streaming usage (with fallback estimation)
            if not prompt_tokens:
                # Fallback: estimate from character count
                # ~4 chars/token for Latin, ~1.5 for CJK (handled by TokenCounter)
                prompt_tokens = self.count_tokens_approx(messages) if messages else 0
                from .token_counter import _cjk_estimate
                completion_tokens = _cjk_estimate(total_text_chars * " ") if total_text_chars > 0 else 0
                completion_tokens = max(1, completion_tokens or total_text_chars // 4)
            else:
                self.last_exact_prompt_tokens = prompt_tokens
            self._total_prompt_tokens += prompt_tokens
            self._total_completion_tokens += completion_tokens
            logger.info("Stream tokens: %d in, %d out (session: %d total)",
                       prompt_tokens, completion_tokens, self.total_tokens)
            if self._usage_callback:
                self._usage_callback(prompt_tokens, completion_tokens)

            if finish_reason:
                yield ("finish", finish_reason)
            break  # success — exit retry loop

    # ── Retry logic ──────────────────────────────────────────────────────

    async def _request_with_retry(self, body: dict[str, Any]) -> dict[str, Any]:
        """Send request with exponential backoff on rate limits."""
        last_error: str | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.post(self._api_url, json=body)
            except (httpx.ConnectError, httpx.ReadTimeout) as e:
                last_error = str(e)
                if attempt < self._max_retries:
                    delay = self._retry_delay(attempt, self._retry_base_delay)
                    logger.warning("Transient network error, retrying in %.1fs: %s", delay, e)
                    await asyncio.sleep(delay)
                    continue
                raise RuntimeError(
                    f"Cannot reach {self._api_url} after {self._max_retries + 1} attempts.\n"
                    f"  Check your connection and the server URL.\n"
                    f"  Detail: {e}"
                )
            except httpx.RemoteProtocolError as e:
                last_error = str(e)
                if attempt < self._max_retries:
                    delay = self._retry_delay(attempt, self._retry_base_delay)
                    logger.warning("Remote protocol error, retrying in %.1fs: %s", delay, e)
                    await asyncio.sleep(delay)
                    continue
                raise RuntimeError(
                    "Server disconnected unexpectedly.\n"
                    "  The model may have timed out internally.\n"
                    "  Try again with a smaller task."
                )

            if response.status_code == 429:
                last_error = "HTTP 429 (rate limited)"
                retry_after = response.headers.get("retry-after", "")
                delay = self._retry_delay(attempt, self._retry_base_delay, retry_after)

                if attempt < self._max_retries:
                    logger.warning(
                        "Rate limited (429). Retrying in %.1fs (attempt %d/%d)...",
                        delay, attempt + 1, self._max_retries,
                    )
                    await asyncio.sleep(delay)
                    continue
                else:
                    raise RuntimeError(
                        f"Rate limit exceeded after {self._max_retries} retries. "
                        f"Wait and try again."
                    )

            if response.status_code >= 500:
                last_error = f"HTTP {response.status_code} (server error)"
                # Server error — retry
                if attempt < self._max_retries:
                    delay = self._retry_delay(attempt, self._retry_base_delay)
                    logger.warning(
                        "Server error (%d). Retrying in %.1fs...",
                        response.status_code, delay,
                    )
                    await asyncio.sleep(delay)
                    continue

            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                # Try to extract error message from response
                try:
                    err_data = response.json()
                    err_msg = err_data.get("error", {}).get("message", str(e))
                except Exception:
                    err_msg = str(e)
                raise RuntimeError(
                    enhance_api_error(response.status_code, f"API error ({response.status_code}): {err_msg}", self.config.base_url)
                )

            return response.json()

        raise RuntimeError(f"Request failed after {self._max_retries} retries: {last_error}")

    # ── Convenience methods ────────────────────────────────────────────────

    async def simple_chat(self, user_message: str, system: str | None = None) -> str:
        """Single-turn chat without tools. Returns text response."""
        messages = [
            {"role": "system", "content": system or SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]
        result = await self.chat(messages, tools=[])
        return result.get("content", "")

    def count_tokens_approx(self, messages: list[Message]) -> int:
        """
        Token count estimation using the unified TokenCounter service.
        Model-aware with per-message caching and CJK fallback.
        """
        from .token_counter import TokenCounter
        return TokenCounter.for_model(self.config.model).count_tokens(messages)

    def set_model(self, model: str) -> None:
        """Change the model at runtime without recreating the client."""
        self.config.model = model

    async def close(self):
        """Close the HTTP client."""
        await self._client.aclose()
