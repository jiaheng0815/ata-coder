"""
Unit tests for llm_client — catches streaming/retry bugs.
Run: python -m pytest tests/ -v
"""

import json
import pytest
from unittest.mock import patch, MagicMock
from ata_coder.llm_client import LLMClient
from ata_coder.config import LLMConfig


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
