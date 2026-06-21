"""
Anthropic Messages API async client — provider-agnostic.

Works with ANY Anthropic-compatible endpoint:
- Native Anthropic:    https://api.anthropic.com
- DeepSeek Anthropic:  https://api.deepseek.com/anthropic
- Any proxy/gateway:   http://localhost:8080

Configuration:
  ATA_CODER_BASE_URL  → base URL (auto-appends /anthropic if needed)
  ATA_CODER_API_KEY   → API key
  ATA_CODER_DEFAULT_MODEL → model name
  ANTHROPIC_MODEL_MAP → optional JSON mapping, e.g. '{"claude-opus":"deepseek-v4-pro"}'
"""

import asyncio
import json
import logging
import os
import random
from typing import Any, AsyncIterator, Callable

import httpx

from .config import LLMConfig
from .types import BaseLLMClient, Message, ToolDef
from .utils import enhance_api_error

logger = logging.getLogger(__name__)


class AnthropicClient(BaseLLMClient):
    """Async HTTP client for Anthropic-compatible Messages API. Provider-agnostic."""

    def __init__(self, config: LLMConfig | None = None):
        self.config = config or LLMConfig()
        self._tools: list[ToolDef] = []
        self._usage_callback: Callable[[int, int], None] | None = None
        self._total_prompt_tokens = 0
        self._total_completion_tokens = 0
        self.last_exact_prompt_tokens: int | None = None

        # ── URL — provider-agnostic ────────────────────────────────────
        base = self.config.base_url.rstrip("/")
        # If the base URL already points to a messages endpoint, use it directly
        if "/messages" in base:
            self._api_url = base
        else:
            # Standard: base_url/v1/messages
            # Auto-add /v1 if the base doesn't already have a version segment.
            # Use regex to avoid false-positives on paths containing "v1"/"v2"
            # as part of a longer segment (e.g. "service-v1-preview").
            import re as _re
            if not _re.search(r'/v\d+', base):
                base += "/v1"
            self._api_url = f"{base}/messages"

        # ── Model — with optional mapping ──────────────────────────────
        self._model = self.config.model
        map_json = ""
        # Read from settings (which includes env block) at init time
        try:
            from .settings import get_settings
            map_json = get_settings().get_env("ANTHROPIC_MODEL_MAP", "")
        except Exception:
            map_json = ""
        if not map_json:
            map_json = os.getenv("ANTHROPIC_MODEL_MAP", "")
        if map_json:
            try:
                model_map = json.loads(map_json)
                if self._model in model_map:
                    self._model = model_map[self._model]
                else:
                    logger.warning("Model %r not found in ANTHROPIC_MODEL_MAP, using as-is", self._model)
            except json.JSONDecodeError:
                logger.warning("ANTHROPIC_MODEL_MAP is invalid JSON, ignoring")

        # ── Headers — Anthropic standard ───────────────────────────────
        self._headers = {
            "x-api-key": self.config.api_key,
            "Content-Type": "application/json",
        }
        # Native Anthropic requires this header (default: 2023-06-01).
        # Proxies may ignore it; override via ANTHROPIC_VERSION env var.
        self._headers["anthropic-version"] = os.getenv("ANTHROPIC_VERSION", "2023-06-01")

        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=30.0),
            headers=self._headers,
        )

        # Retry config
        self._max_retries = 3
        self._retry_base_delay = 1.0  # seconds

    def on_usage(self, callback: Callable[[int, int], None]) -> None:
        self._usage_callback = callback

    def register_tools(self, tools: list[ToolDef]) -> None:
        """Convert OpenAI-format tools to Anthropic format."""
        result = []
        for t in tools:
            fn = t.get("function", t)
            result.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })
        self._tools = result

    @property
    def total_prompt_tokens(self) -> int: return self._total_prompt_tokens
    @property
    def total_completion_tokens(self) -> int: return self._total_completion_tokens
    @property
    def total_tokens(self) -> int: return self._total_prompt_tokens + self._total_completion_tokens

    # ═════════════════════════════════════════════════════════════════════
    # Chat (non-streaming)
    # ═════════════════════════════════════════════════════════════════════

    async def chat(self, messages: list[Message], system_prompt: str = "",
                   tools: list[ToolDef] | None = None) -> Message:
        tool_defs = tools if tools is not None else self._tools
        anthropic_msgs, system = self._convert_messages(messages, system_prompt)

        body: dict[str, Any] = {
            "model": self._model,
            "messages": anthropic_msgs,
            "max_tokens": self.config.max_tokens,
        }
        if system:
            body["system"] = system
        if tool_defs:
            body["tools"] = tool_defs

        self._apply_thinking(body)

        # Sanitize surrogates before JSON encoding (prevent UTF-8 encode crash)
        from .utils import sanitize_surrogates
        body = sanitize_surrogates(body)

        logger.debug("Anthropic %s: %d msgs, %d tools", self._model,
                     len(anthropic_msgs), len(tool_defs) if tool_defs else 0)

        return await self._request_with_retry(body)

    async def _request_with_retry(self, body: dict[str, Any]) -> Message:
        """Send request with exponential backoff on rate limits / server errors."""
        last_error: str | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._client.post(self._api_url, json=body)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
                last_error = str(e)
                if attempt < self._max_retries:
                    delay = self._retry_delay(attempt, self._retry_base_delay)
                    logger.warning("Anthropic connect error, retrying in %.1fs: %s", delay, e)
                    await asyncio.sleep(delay)
                    continue
                raise RuntimeError(f"Cannot reach Anthropic API: {e}") from e

            if resp.status_code == 429:
                last_error = "HTTP 429 (rate limited)"
                if attempt < self._max_retries:
                    retry_after = resp.headers.get("retry-after", "")
                    delay = self._retry_delay(attempt, self._retry_base_delay, retry_after)
                    logger.warning("Anthropic rate limited (429), retrying in %.1fs (attempt %d/%d)",
                                   delay, attempt + 1, self._max_retries)
                    await asyncio.sleep(delay)
                    continue
                raise RuntimeError(f"Rate limit exceeded after {self._max_retries} retries")

            if resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code} (server error)"
                if attempt < self._max_retries:
                    delay = self._retry_delay(attempt, self._retry_base_delay)
                    logger.warning("Anthropic server error (%d), retrying in %.1fs", resp.status_code, delay)
                    await asyncio.sleep(delay)
                    continue

            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                try:
                    err_data = resp.json()
                    err_msg = err_data.get("error", {}).get("message", str(e))
                except Exception:
                    err_msg = str(e)
                raise RuntimeError(
                    enhance_api_error(resp.status_code, f"Anthropic API error ({resp.status_code}): {err_msg}", self.config.base_url)
                ) from e

            return self._convert_response(resp.json())

        raise RuntimeError(f"Request failed after {self._max_retries} retries: {last_error}")

    # ═════════════════════════════════════════════════════════════════════
    # Chat (streaming)
    # ═════════════════════════════════════════════════════════════════════

    async def chat_stream(self, messages: list[Message], system_prompt: str = "",
                          tools: list[ToolDef] | None = None) -> AsyncIterator[tuple[str, Any]]:
        tool_defs = tools if tools is not None else self._tools
        anthropic_msgs, system = self._convert_messages(messages, system_prompt)

        body: dict[str, Any] = {
            "model": self._model,
            "messages": anthropic_msgs,
            "max_tokens": self.config.max_tokens,
            "stream": True,
        }
        if system:
            body["system"] = system
        if tool_defs:
            body["tools"] = tool_defs

        self._apply_thinking(body)

        # Sanitize surrogates before JSON encoding (prevent UTF-8 encode crash)
        from .utils import sanitize_surrogates
        body = sanitize_surrogates(body)

        # Retry loop for streaming (up to 2 retries for 429/5xx)
        for attempt in range(self._max_retries):
            try:
                resp = await self._client.send(
                    self._client.build_request("POST", self._api_url, json=body),
                    stream=True,
                )
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
                if attempt < self._max_retries - 1:
                    delay = self._retry_delay(attempt, self._retry_base_delay)
                    logger.warning("Anthropic stream connect error, retrying in %.1fs: %s", delay, e)
                    await asyncio.sleep(delay)
                    continue
                raise RuntimeError(f"Stream connection failed: {e}") from e

            if resp.status_code >= 400:
                if resp.status_code == 429 and attempt < self._max_retries - 1:
                    delay = self._retry_delay(attempt, self._retry_base_delay)
                    logger.warning("Anthropic stream rate limited, retrying in %.1fs", delay)
                    await asyncio.sleep(delay)
                    continue
                if resp.status_code >= 500 and attempt < self._max_retries - 1:
                    delay = self._retry_delay(attempt, self._retry_base_delay)
                    logger.warning("Anthropic stream server error (%d), retrying in %.1fs", resp.status_code, delay)
                    await asyncio.sleep(delay)
                    continue
                try:
                    error_body_raw = (await resp.aread()).decode("utf-8", errors="replace")[:500]
                except Exception:
                    error_body_raw = "(could not read body)"
                # Extract API error message from JSON body for richer diagnostics
                try:
                    err_data = json.loads(error_body_raw)
                    err_msg = err_data.get("error", {}).get("message", error_body_raw)
                except (json.JSONDecodeError, AttributeError):
                    err_msg = error_body_raw
                logger.error("Anthropic stream request failed (%d): %s", resp.status_code, err_msg[:200])
                raise RuntimeError(
                    enhance_api_error(
                        resp.status_code,
                        f"Anthropic API error ({resp.status_code}): {err_msg}",
                        self.config.base_url,
                    )
                )

            tool_buf: dict[int, dict] = {}
            prompt_tokens = 0
            completion_tokens = 0
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                evt_type = event.get("type", "")
                delta = event.get("delta", {})
                idx = event.get("index", 0)

                # Track usage from streaming events (Anthropic protocol)
                if evt_type == "message_start":
                    msg = event.get("message", {})
                    usage = msg.get("usage", {})
                    if usage.get("input_tokens"):
                        prompt_tokens = usage["input_tokens"]
                elif evt_type == "message_delta":
                    usage = delta.get("usage", {})
                    if usage.get("output_tokens"):
                        completion_tokens = usage["output_tokens"]

                if evt_type == "content_block_delta":
                    dt = delta.get("type", "")
                    if dt == "text_delta":
                        yield ("text", delta.get("text", ""))
                    elif dt == "thinking_delta":
                        yield ("reasoning", delta.get("thinking", ""))
                    elif dt == "input_json_delta":
                        if idx not in tool_buf:
                            tool_buf[idx] = {"id": "", "name": "", "arguments": ""}
                        tool_buf[idx]["arguments"] += delta.get("partial_json", "")

                elif evt_type == "content_block_start":
                    block = event.get("content_block", {})
                    if block.get("type") == "tool_use":
                        tool_buf[idx] = {"id": block.get("id", ""), "name": block.get("name", ""), "arguments": ""}

                elif evt_type == "message_stop":
                    yield ("finish", "end_turn")

            # Update token counters with streamed usage data
            if prompt_tokens:
                self._total_prompt_tokens += prompt_tokens
                self.last_exact_prompt_tokens = prompt_tokens
            if completion_tokens:
                self._total_completion_tokens += completion_tokens
            if self._usage_callback and (prompt_tokens or completion_tokens):
                self._usage_callback(prompt_tokens, completion_tokens)

            # Yield tool calls
            for idx in sorted(tool_buf.keys()):
                buf = tool_buf[idx]
                args = buf["arguments"]
                if args:
                    try:
                        json.loads(args)
                    except json.JSONDecodeError:
                        args = self._balance_json(args)
                yield ("tool_call", {
                    "id": buf["id"], "type": "function",
                    "function": {"name": buf["name"], "arguments": args},
                })
            break  # success — exit retry loop

    # ═════════════════════════════════════════════════════════════════════
    # Internal helpers
    # ═════════════════════════════════════════════════════════════════════

    @staticmethod
    def _balance_json(text: str) -> str:
        """Complete a truncated JSON string by appending missing closing brackets.

        Handles nested objects, arrays, and string literals — not just a single
        trailing ``}`` like the old ``args += "}"`` hack that failed on nested
        structures and partial arrays.
        """
        pairs = {'{': '}', '[': ']'}
        stack: list[str] = []
        in_string = False
        escape = False
        for ch in text:
            if escape:
                escape = False
                continue
            if ch == '\\':
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch in pairs:
                stack.append(pairs[ch])
            elif ch in (']', '}'):
                if stack and stack[-1] == ch:
                    stack.pop()
        # Close any unterminated string before balancing brackets
        result = text
        if in_string and not escape:
            result += '"'
        return result + ''.join(reversed(stack))

    def _apply_thinking(self, body: dict) -> None:
        """Apply thinking/reasoning_effort — provider-agnostic.

        NOTE: DeepSeek supports low/medium/high/max. The Anthropic format
        only recognises low/high. Values are passed through as-is; it is
        the caller's responsibility to choose a supported strength.
        """
        strength = getattr(self.config, 'thinking_strength', '') or ''
        if not strength or strength.lower() == 'off':
            return
        # Anthropic format: thinking type + output_config
        body["thinking"] = {"type": "enabled"}
        body["output_config"] = {"effort": strength.lower()}

    def _convert_messages(self, openai_msgs: list[Message], system_prompt: str = "") -> tuple[list[dict], str]:
        """OpenAI-format messages → Anthropic-format."""
        result = []
        system = system_prompt

        for msg in openai_msgs:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "system":
                system = (system + "\n\n" + content).strip() if content else system
                continue

            if role == "user":
                result.append({"role": "user", "content": content or ""})

            elif role == "assistant":
                blocks = []
                if msg.get("reasoning_content"):
                    blocks.append({"type": "thinking", "thinking": msg["reasoning_content"]})
                if content:
                    blocks.append({"type": "text", "text": content})
                for tc in msg.get("tool_calls", []):
                    fn = tc.get("function", {})
                    inp = fn.get("arguments", "{}")
                    if isinstance(inp, str):
                        try:
                            inp = json.loads(inp)
                        except json.JSONDecodeError:
                            inp = {}
                    blocks.append({"type": "tool_use", "id": tc.get("id", ""),
                                   "name": fn.get("name", ""), "input": inp})
                result.append({"role": "assistant", "content": blocks if blocks else content or ""})

            elif role == "tool":
                result.append({"role": "user", "content": [{
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": content or "",
                }]})

        return result, system

    def _convert_response(self, data: dict) -> Message:
        """Anthropic response → OpenAI-format message."""
        result: Message = {"role": "assistant", "content": ""}
        texts, tools, reasonings = [], [], []

        for block in data.get("content", []):
            t = block.get("type", "")
            if t == "text":
                texts.append(block.get("text", ""))
            elif t == "tool_use":
                tools.append({"id": block.get("id", ""), "type": "function",
                              "function": {"name": block.get("name", ""),
                                           "arguments": json.dumps(block.get("input", {}))}})
            elif t == "thinking":
                reasonings.append(block.get("thinking", ""))

        result["content"] = "\n".join(texts)
        if tools:
            result["tool_calls"] = tools
        if reasonings:
            result["reasoning_content"] = "\n".join(reasonings)

        usage = data.get("usage", {})
        inp = usage.get("input_tokens", 0)
        out = usage.get("output_tokens", 0)
        if inp:
            self.last_exact_prompt_tokens = inp
        if not out and texts:
            from .token_counter import _cjk_estimate
            out_text = "\n".join(texts)
            out = max(1, _cjk_estimate(out_text))
        self._total_prompt_tokens += inp
        self._total_completion_tokens += out
        if self._usage_callback:
            self._usage_callback(inp, out)

        return result

    def count_tokens_approx(self, messages: list[Message]) -> int:
        """Token count estimation using the unified TokenCounter service."""
        from .token_counter import TokenCounter
        return TokenCounter.for_model(self.config.model).count_tokens(messages)

    def set_model(self, model: str) -> None:
        """Change the model at runtime without recreating the client."""
        self.config.model = model
        self._model = model

    async def close(self):
        await self._client.aclose()
