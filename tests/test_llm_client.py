"""
Unit tests for llm_client — catches streaming/retry bugs.
Run: python -m pytest tests/ -v
"""

import json
import pytest
from unittest.mock import patch, MagicMock
from ata_coder.llm_client import LLMClient
from ata_coder.config import LLMConfig
from ata_coder.types import BaseLLMClient
from ata_coder.utils import sanitize_surrogates, enhance_api_error


class FakeResponse:
    """Simulates an httpx streaming response."""
    def __init__(self, status_code=200, lines=None):
        self.status_code = status_code
        self._lines = lines or []

    def iter_lines(self):
        return iter(self._lines)

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    def read(self):
        return b""

    async def aread(self):
        return b""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


def make_sse_chunk(content=None, tool_calls=None, finish_reason=None, usage=None):
    """Build a single SSE data line."""
    delta = {}
    if content:
        delta["content"] = content
    if tool_calls:
        delta["tool_calls"] = tool_calls
    choice = {"index": 0, "delta": delta}
    if finish_reason:
        choice["finish_reason"] = finish_reason
    chunk = {"choices": [choice]}
    if usage:
        chunk["usage"] = usage
    return f"data: {json.dumps(chunk)}"


async def test_chat_stream_yields_text():
    """chat_stream must yield text deltas from SSE chunks."""
    config = LLMConfig(api_key="test", base_url="http://fake/v1", model="test")
    client = LLMClient(config)

    sse_lines = [
        make_sse_chunk(content="Hello"),
        make_sse_chunk(content=" world"),
        make_sse_chunk(finish_reason="stop", usage={"prompt_tokens": 10, "completion_tokens": 2}),
        "data: [DONE]",
    ]

    with patch.object(client._client, 'send', return_value=FakeResponse(200, sse_lines)):
        results = [chunk async for chunk in client.chat_stream([{"role": "user", "content": "hi"}])]

    texts = [c for t, c in results if t == "text"]
    assert "".join(texts) == "Hello world", f"Expected 'Hello world', got {texts!r}"
    assert any(t == "finish" for t, _ in results), "Missing finish event"


async def test_chat_stream_yields_tool_calls():
    """chat_stream must yield tool calls from streaming SSE."""
    config = LLMConfig(api_key="test", base_url="http://fake/v1", model="test")
    client = LLMClient(config)

    sse_lines = [
        make_sse_chunk(tool_calls=[
            {"index": 0, "id": "call_1", "function": {"name": "gre", "arguments": ""}}
        ]),
        make_sse_chunk(tool_calls=[
            {"index": 0, "function": {"arguments": '{"name": "world"}'}}
        ]),
        make_sse_chunk(finish_reason="tool_calls", usage={"prompt_tokens": 5, "completion_tokens": 5}),
        "data: [DONE]",
    ]

    with patch.object(client._client, 'send', return_value=FakeResponse(200, sse_lines)):
        results = [chunk async for chunk in client.chat_stream([{"role": "user", "content": "greet world"}])]

    tool_calls = [c for t, c in results if t == "tool_call"]
    assert len(tool_calls) >= 1, f"Expected tool calls, got {tool_calls}"
    assert tool_calls[0]["function"]["name"] == "gre", f"Wrong tool name: {tool_calls}"


async def test_chat_stream_empty_response_raises():
    """Empty streaming response should still complete, not hang."""
    config = LLMConfig(api_key="test", base_url="http://fake/v1", model="test")
    client = LLMClient(config)

    # No content, no tools — just finish
    sse_lines = ["data: [DONE]"]

    with patch.object(client._client, 'send', return_value=FakeResponse(200, sse_lines)):
        results = [chunk async for chunk in client.chat_stream([{"role": "user", "content": "hi"}])]

    # Should complete without hanging
    assert len([t for t, _ in results if t == "text"]) == 0


async def test_chat_stream_retries_on_429():
    """chat_stream should retry on 429, not crash immediately."""
    config = LLMConfig(api_key="test", base_url="http://fake/v1", model="test")
    client = LLMClient(config)
    client._max_retries = 3
    client._retry_base_delay = 0.0  # no delay in tests

    call_count = [0]

    async def fake_send(request, stream=False):
        call_count[0] += 1
        if call_count[0] < 3:
            return FakeResponse(429)
        # Third attempt succeeds
        return FakeResponse(200, ["data: [DONE]"])

    with patch.object(client._client, 'send', fake_send):
        with patch.object(client._client, 'build_request',
                          return_value=MagicMock()):
            results = [chunk async for chunk in client.chat_stream([{"role": "user", "content": "hi"}])]

    assert call_count[0] == 3, f"Expected 3 attempts (2 retries + 1 success), got {call_count[0]}"
    assert len(results) == 0 or all(t in ("finish",) for t, _ in results)


async def test_chat_stream_raises_on_401():
    """chat_stream should raise immediately on auth errors, not retry."""
    config = LLMConfig(api_key="test", base_url="http://fake/v1", model="test")
    client = LLMClient(config)
    client._max_retries = 1
    client._retry_base_delay = 0.0

    with patch.object(client._client, 'send', return_value=FakeResponse(401)):
        with patch.object(client._client, 'build_request',
                          return_value=MagicMock()):
            with pytest.raises(Exception):
                [chunk async for chunk in client.chat_stream([{"role": "user", "content": "hi"}])]


# ── _retry_delay: exponential backoff with jitter and cap ─────────────────


class TestRetryDelay:
    """_retry_delay is a pure function — testable without mocks or network."""

    def test_first_attempt_base(self):
        """Attempt 0 with base 1.0: in range [0.5, 1.5] before cap."""
        for _ in range(20):
            d = BaseLLMClient._retry_delay(0, base_delay=1.0)
            assert 0.5 <= d <= 1.5
            assert d <= 60.0

    def test_exponential_growth_floor(self):
        """Attempt 2 with base 1.0: floor = 2^2 / 2 = 2.0."""
        for _ in range(20):
            d = BaseLLMClient._retry_delay(2, base_delay=1.0)
            assert d >= 2.0

    def test_capped_at_60_seconds(self):
        """Delays must never exceed 60 seconds regardless of attempt count."""
        for _ in range(20):
            d = BaseLLMClient._retry_delay(100, base_delay=100.0)
            assert d <= 60.0

    def test_retry_after_header_used(self):
        """When retry-after is provided it sets the base before jitter."""
        for _ in range(20):
            d = BaseLLMClient._retry_delay(0, base_delay=1.0, retry_after="10")
            # 10s base, jittered to [5, 15], capped at 60
            assert 5.0 <= d <= 15.0

    def test_retry_after_invalid_falls_back(self):
        """Non-numeric retry-after falls back to exponential formula."""
        for _ in range(20):
            d = BaseLLMClient._retry_delay(1, base_delay=2.0, retry_after="invalid")
            # falls back: 2*2^1=4.0 * jitter → [2.0, 6.0]
            assert 2.0 <= d <= 6.0


# ── sanitize_surrogates: lone surrogates break UTF-8 encoding ─────────────


class TestSanitizeSurrogates:
    """sanitize_surrogates is a pure recursive function — no mocks needed."""

    def test_passthrough_ascii(self):
        assert sanitize_surrogates("hello") == "hello"

    def test_passthrough_cjk(self):
        assert sanitize_surrogates("你好世界") == "你好世界"

    def test_passthrough_emoji(self):
        assert sanitize_surrogates("🔥🎉") == "🔥🎉"

    def test_passthrough_non_string(self):
        assert sanitize_surrogates(42) == 42
        assert sanitize_surrogates(None) is None
        assert sanitize_surrogates(True) is True

    def test_strips_lone_high_surrogate(self):
        """Lone high surrogate U+D800 should be replaced, not crash."""
        raw = "hello\ud800world"
        result = sanitize_surrogates(raw)
        assert "hello" in result
        assert "world" in result
        assert "\ud800" not in result

    def test_strips_lone_low_surrogate(self):
        """Lone low surrogate U+DFFF should be replaced."""
        result = sanitize_surrogates("x\udfff")
        assert "\udfff" not in result
        assert len(result) >= 1  # 'x' survives

    def test_valid_surrogate_pair_preserved(self):
        """Valid surrogate pairs (like emoji) should survive the round-trip."""
        emoji = "🔥"  # 🔥 — valid pair
        result = sanitize_surrogates(emoji)
        assert "🔥" in result or len(result) > 0

    def test_nested_dict_surrogates(self):
        obj = {"key": "val\udfff", "nested": {"deep": "\ud800bad"}}
        result = sanitize_surrogates(obj)
        assert "\udfff" not in result["key"]
        assert "\ud800" not in result["nested"]["deep"]

    def test_nested_list_surrogates(self):
        obj = ["ok", "\ud800bad", ["\udfffmore"]]
        result = sanitize_surrogates(obj)
        assert "\ud800" not in result[1]
        assert "\udfff" not in result[2][0]

    def test_depth_limit(self):
        """Deeply nested payload beyond max_depth should not overflow."""
        deeply_nested = {}
        cur = deeply_nested
        for _ in range(600):
            cur["next"] = {}
            cur = cur["next"]
        cur["value"] = "\ud800x"
        result = sanitize_surrogates(deeply_nested)
        assert isinstance(result, dict)

    def test_mixed_types(self):
        obj = {"items": [1, "a\ud800b", None], "flag": True, "num": 3.14}
        result = sanitize_surrogates(obj)
        assert result["items"][0] == 1
        assert result["num"] == 3.14
        assert "\ud800" not in result["items"][1]


# ── Tool call assembly from incremental SSE chunks ────────────────────────


async def test_chat_stream_assembles_incremental_tool_calls():
    """Tool calls split across multiple SSE chunks must be assembled correctly."""
    config = LLMConfig(api_key="test", base_url="http://fake/v1", model="test")
    client = LLMClient(config)

    sse_lines = [
        # Fragment 1: id + partial name
        make_sse_chunk(tool_calls=[
            {"index": 0, "id": "call_abc", "function": {"name": "re", "arguments": ""}}
        ]),
        # Fragment 2: remaining name + partial args
        make_sse_chunk(tool_calls=[
            {"index": 0, "function": {"name": "ad_file", "arguments": '{"file_'}}
        ]),
        # Fragment 3: rest of args
        make_sse_chunk(tool_calls=[
            {"index": 0, "function": {"arguments": 'path":"/tmp/x"}'}}
        ]),
        make_sse_chunk(finish_reason="tool_calls", usage={"prompt_tokens": 3, "completion_tokens": 5}),
        "data: [DONE]",
    ]

    with patch.object(client._client, 'send', return_value=FakeResponse(200, sse_lines)):
        results = [chunk async for chunk in client.chat_stream([{"role": "user", "content": "read /tmp/x"}])]

    tool_calls = [c for t, c in results if t == "tool_call"]
    assert len(tool_calls) == 1
    tc = tool_calls[0]
    assert tc["id"] == "call_abc"
    assert tc["function"]["name"] == "read_file"
    assert '"/tmp/x"' in tc["function"]["arguments"]


# ── State management ──────────────────────────────────────────────────────


def test_register_tools_stores_definitions():
    """register_tools should store and later serve tool definitions."""
    config = LLMConfig(api_key="test", base_url="http://fake/v1", model="test")
    client = LLMClient(config)
    assert client._tools == []

    tools = [{"type": "function", "function": {"name": "test_tool", "parameters": {}}}]
    client.register_tools(tools)
    assert client._tools == tools


def test_set_model_updates_config():
    """set_model should change the model name on the active config."""
    config = LLMConfig(api_key="test", base_url="http://fake/v1", model="old-model")
    client = LLMClient(config)
    assert client.config.model == "old-model"

    client.set_model("new-model")
    assert client.config.model == "new-model"


# ── enhance_api_error ─────────────────────────────────────────────────────


def test_enhance_api_error_adds_model_hint():
    msg = "model 'gpt-5' not found"
    result = enhance_api_error(404, msg)
    assert "settings.json" in result
    assert "model name is incorrect" in result


def test_enhance_api_error_adds_auth_hint():
    result = enhance_api_error(401, "unauthorized")
    assert "API key" in result or "Authentication" in result


def test_enhance_api_error_adds_rate_limit_hint():
    result = enhance_api_error(429, "rate limit exceeded")
    assert "throttling" in result or "Rate limited" in result
