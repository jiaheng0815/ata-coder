"""
OpenAI-compatible LLM client with tool/function calling support.
Uses httpx directly (no openai SDK dependency) for maximum compatibility.
Supports any provider that implements the OpenAI chat completions API format.
"""

import json
import logging
import re
import time
from typing import Any, Callable, Iterator

import httpx

from .config import LLMConfig

logger = logging.getLogger(__name__)


# ── Tool / Message type definitions ──────────────────────────────────────────

ToolDef = dict[str, Any]          # OpenAI tool definition dict
Message = dict[str, Any]           # OpenAI message dict


# ── System prompt for the coding agent ───────────────────────────────────────

def _load_system_prompt() -> str:
    """Load fallback system prompt from skills/codecraft/SKILL.md if available."""
    import re
    from pathlib import Path
    # Try new location: skills/codecraft/SKILL.md
    prompt_file = Path(__file__).parent / "skills" / "codecraft" / "SKILL.md"
    if prompt_file.exists():
        try:
            raw = prompt_file.read_text(encoding="utf-8")
            # Strip YAML frontmatter to get the prompt body
            match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", raw, re.DOTALL)
            if match:
                return match.group(2).strip()
            return raw
        except Exception:
            pass
    # Fallback
    return "You are an expert software engineer. Write correct, secure, maintainable code."

SYSTEM_PROMPT = _load_system_prompt()


class LLMClient:
    """
    OpenAI-compatible LLM client using httpx directly.

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

        self._client = httpx.Client(
            timeout=httpx.Timeout(300.0, connect=30.0),
            headers=self._headers,
        )

        # Usage tracking
        self._usage_callback: Callable[[int, int], None] | None = None
        self._total_prompt_tokens = 0
        self._total_completion_tokens = 0

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

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
    ) -> Message:
        """
        Send messages and get a completion.
        Returns the assistant message (may include tool_calls).
        Automatically retries on rate limit (429) errors.
        """
        tool_defs = tools if tools is not None else self._tools

        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }

        if tool_defs:
            body["tools"] = tool_defs
            body["tool_choice"] = "auto"

        # Thinking mode
        # xhigh is an alias for max (some UIs use this name)
        _THINKING_MAP = {"xhigh": "max"}
        thinking_strength = getattr(self.config, 'thinking_strength', '') or ''
        if thinking_strength and thinking_strength.lower() != 'off':
            strength = thinking_strength.lower()
            body["reasoning_effort"] = _THINKING_MAP.get(strength, strength)
            body.pop("temperature", None)
        elif getattr(self.config, 'thinking_disabled', False):
            body["extra_body"] = {"thinking": {"type": "disabled"}}

        logger.debug(
            "Calling %s with %d messages, %d tools, thinking=%s",
            self.config.model,
            len(messages),
            len(tool_defs) if tool_defs else 0,
            thinking_strength or "off",
        )

        data = self._request_with_retry(body)

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

    def chat_stream(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
    ) -> Iterator[tuple[str, Any]]:
        """
        Stream a chat completion.
        Yields (delta_type, content) tuples.
        delta_type is one of: "text", "tool_call", "finish".

        Example:
            for delta_type, content in client.chat_stream(messages):
                if delta_type == "text":
                    print(content, end="")
                elif delta_type == "tool_call":
                    print(f"Tool: {content}")
                elif delta_type == "finish":
                    print(f"Done: {content}")
        """
        tool_defs = tools if tools is not None else self._tools

        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": True,
        }

        if tool_defs:
            body["tools"] = tool_defs
            body["tool_choice"] = "auto"

        # Thinking mode for streaming
        _THINKING_MAP = {"xhigh": "max"}
        thinking_strength = getattr(self.config, 'thinking_strength', '') or ''
        if thinking_strength and thinking_strength.lower() != 'off':
            strength = thinking_strength.lower()
            body["reasoning_effort"] = _THINKING_MAP.get(strength, strength)
            body.pop("temperature", None)
        elif getattr(self.config, 'thinking_disabled', False):
            body["extra_body"] = {"thinking": {"type": "disabled"}}

        # Retry loop for streaming (up to 2 retries for 429/5xx)
        last_error = None
        for attempt in range(self._max_retries):
            try:
                response = self._client.send(
                    self._client.build_request("POST", self._api_url, json=body),
                    stream=True,
                )
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
                if attempt < self._max_retries - 1:
                    delay = self._retry_base_delay * (2 ** attempt)
                    logger.warning("Stream connect error, retrying in %.1fs: %s", delay, e)
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"Stream connection failed: {e}")

            if response.status_code >= 400:
                if response.status_code == 429 and attempt < self._max_retries - 1:
                    delay = self._retry_base_delay * (2 ** attempt)
                    logger.warning("Stream rate limited, retrying in %.1fs", delay)
                    time.sleep(delay)
                    continue
                if response.status_code >= 500 and attempt < self._max_retries - 1:
                    delay = self._retry_base_delay * (2 ** attempt)
                    logger.warning("Stream server error (%d), retrying in %.1fs", response.status_code, delay)
                    time.sleep(delay)
                    continue
                try:
                    error_body = response.read().decode("utf-8", errors="replace")[:500]
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

            for line in response.iter_lines():
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
                # Roughly 4 chars per token for English, 1.5 for Chinese
                prompt_tokens = self.count_tokens_approx(messages) if messages else 0
                completion_tokens = max(1, total_text_chars // 3)
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

    def _request_with_retry(self, body: dict[str, Any]) -> dict[str, Any]:
        """Send request with exponential backoff on rate limits."""
        last_error = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.post(self._api_url, json=body)
            except httpx.ConnectError as e:
                raise RuntimeError(
                    f"Cannot connect to {self._api_url}\n"
                    f"  Check: is the server running? Is the URL correct?\n"
                    f"  Current: {self.config.base_url}\n"
                    f"  Detail: {e}"
                )
            except httpx.ReadTimeout:
                raise RuntimeError(
                    f"Request timed out after 300s.\n"
                    f"  The model may be overloaded or the prompt too large.\n"
                    f"  Try again or reduce the task complexity."
                )
            except httpx.RemoteProtocolError as e:
                if attempt < self._max_retries:
                    delay = self._retry_base_delay * (2 ** attempt)
                    logger.warning("Remote protocol error, retrying in %.1fs: %s", delay, e)
                    time.sleep(delay)
                    continue
                raise RuntimeError(
                    f"Server disconnected unexpectedly.\n"
                    f"  The model may have timed out internally.\n"
                    f"  Try again with a smaller task."
                )

            if response.status_code == 429:
                # Rate limited — extract retry-after or use exponential backoff
                retry_after = response.headers.get("retry-after", "")
                try:
                    delay = float(retry_after) if retry_after else self._retry_base_delay * (2 ** attempt)
                except ValueError:
                    delay = self._retry_base_delay * (2 ** attempt)
                delay = min(delay, 60.0)  # cap at 60s

                if attempt < self._max_retries:
                    logger.warning(
                        "Rate limited (429). Retrying in %.1fs (attempt %d/%d)...",
                        delay, attempt + 1, self._max_retries,
                    )
                    time.sleep(delay)
                    continue
                else:
                    raise RuntimeError(
                        f"Rate limit exceeded after {self._max_retries} retries. "
                        f"Wait and try again."
                    )

            if response.status_code >= 500:
                # Server error — retry
                if attempt < self._max_retries:
                    delay = self._retry_base_delay * (2 ** attempt)
                    logger.warning(
                        "Server error (%d). Retrying in %.1fs...",
                        response.status_code, delay,
                    )
                    time.sleep(delay)
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
                raise RuntimeError(f"API error ({response.status_code}): {err_msg}")

            return response.json()

        raise RuntimeError(f"Request failed after {self._max_retries} retries: {last_error}")

    # ── Convenience methods ────────────────────────────────────────────────

    def simple_chat(self, user_message: str, system: str | None = None) -> str:
        """Single-turn chat without tools. Returns text response."""
        messages = [
            {"role": "system", "content": system or SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]
        result = self.chat(messages, tools=[])
        return result.get("content", "")

    def count_tokens_approx(self, messages: list[Message]) -> int:
        """
        Token count estimation — CJK-aware + tiktoken if available.
        """
        try:
            import tiktoken
            enc = tiktoken.get_encoding("cl100k_base")
            total = 0
            for msg in messages:
                content = msg.get("content", "") or ""
                total += len(enc.encode(content))
                for tc in msg.get("tool_calls", []):
                    total += len(enc.encode(json.dumps(tc)))
            return total
        except ImportError:
            pass

        # CJK-aware fallback
        import re
        total = 0
        for msg in messages:
            content = msg.get("content", "") or ""
            cjk = len(re.findall(r'[一-鿿　-〿＀-￯]', content))
            other = len(content) - cjk
            total += (cjk * 2 // 3) + (other // 4)
            for tc in msg.get("tool_calls", []):
                total += len(json.dumps(tc)) // 4
        return max(1, total)

    def set_model(self, model: str) -> None:
        """Change the model at runtime without recreating the client."""
        self.config.model = model

    def close(self):
        """Close the HTTP client."""
        self._client.close()
