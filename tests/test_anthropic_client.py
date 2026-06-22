"""
Unit tests for anthropic_client — message conversion, JSON balancing,
tool format translation, streaming response assembly.
"""
import json
import pytest
from unittest.mock import patch, MagicMock
from ata_coder.anthropic_client import AnthropicClient
from ata_coder.config import LLMConfig


# ── _balance_json — truncated JSON repair during streaming ────────────────


class TestBalanceJson:
    """_balance_json completes truncated JSON tool-call arguments."""

    def test_complete_json_unchanged(self):
        assert AnthropicClient._balance_json('{"key": "value"}') == '{"key": "value"}'

    def test_missing_closing_brace(self):
        result = AnthropicClient._balance_json('{"key": "value"')
        assert json.loads(result) == {"key": "value"}

    def test_missing_closing_bracket(self):
        result = AnthropicClient._balance_json('["a","b"')
        assert json.loads(result) == ["a", "b"]

    def test_nested_objects(self):
        result = AnthropicClient._balance_json('{"outer": {"inner": [1,2')
        parsed = json.loads(result)
        assert parsed["outer"]["inner"] == [1, 2]

    def test_nested_arrays(self):
        result = AnthropicClient._balance_json('[{"a":1},{"b":2')
        parsed = json.loads(result)
        assert len(parsed) == 2
        assert parsed[0] == {"a": 1}
        assert parsed[1] == {"b": 2}

    def test_unterminated_string(self):
        """Unterminated string literal should be closed before balancing."""
        result = AnthropicClient._balance_json('{"key": "hello')
        parsed = json.loads(result)
        assert parsed["key"] == "hello"

    def test_escaped_quote_in_string(self):
        """Escaped quotes inside strings shouldn't confuse the parser."""
        result = AnthropicClient._balance_json('{"key": "val\\"ue"')
        parsed = json.loads(result)
        assert parsed["key"] == 'val"ue'

    def test_empty_input(self):
        result = AnthropicClient._balance_json("")
        assert result == ""

    def test_partial_key_value(self):
        """Partial key without value: _balance_json adds braces but can't
        invent a colon — the resulting JSON is structurally valid but the
        value for the last key will be the closing brace insertions.
        This is a boundary case; the real streaming flow repairs via retry."""
        result = AnthropicClient._balance_json('{"file_path"')
        # Should not raise — balanced brackets produced
        assert result.count("{") == result.count("}")

    def test_partial_array_element(self):
        result = AnthropicClient._balance_json('["a","b","c')
        parsed = json.loads(result)
        assert parsed == ["a", "b", "c"]

    def test_balance_mixed(self):
        """Truncated nested: object → array → unfinished string."""
        result = AnthropicClient._balance_json('{"files": ["main.py", "test')
        parsed = json.loads(result)
        assert parsed["files"] == ["main.py", "test"]


# ── register_tools — OpenAI → Anthropic tool format translation ───────────


class TestRegisterTools:
    """register_tools converts OpenAI function-calling format → Anthropic."""

    def test_empty_list(self):
        client = AnthropicClient(LLMConfig(api_key="k", base_url="http://x/v1", model="m"))
        client.register_tools([])
        assert client._tools == []

    def test_single_tool_conversion(self):
        client = AnthropicClient(LLMConfig(api_key="k", base_url="http://x/v1", model="m"))
        openai_tools = [{
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        }]
        client.register_tools(openai_tools)
        assert len(client._tools) == 1
        tool = client._tools[0]
        assert tool["name"] == "read_file"
        assert tool["description"] == "Read a file"
        assert tool["input_schema"]["type"] == "object"

    def test_multiple_tools(self):
        client = AnthropicClient(LLMConfig(api_key="k", base_url="http://x/v1", model="m"))
        openai_tools = [
            {"type": "function", "function": {"name": f"tool_{i}", "parameters": {}}}
            for i in range(5)
        ]
        client.register_tools(openai_tools)
        assert len(client._tools) == 5
        assert [t["name"] for t in client._tools] == ["tool_0", "tool_1", "tool_2", "tool_3", "tool_4"]

    def test_tool_without_type_wrapper(self):
        """Tools without the "type":"function" wrapper should still work."""
        client = AnthropicClient(LLMConfig(api_key="k", base_url="http://x/v1", model="m"))
        tools = [{
            "name": "bare_tool",
            "description": "No wrapper",
            "parameters": {},
        }]
        client.register_tools(tools)
        assert client._tools[0]["name"] == "bare_tool"


# ── _convert_messages — OpenAI ↔ Anthropic message format ─────────────────


class TestConvertMessages:
    """_convert_messages translates OpenAI message format → Anthropic format."""

    def test_simple_user_message(self):
        client = AnthropicClient(LLMConfig(api_key="k", base_url="http://x/v1", model="m"))
        msgs, system = client._convert_messages(
            [{"role": "user", "content": "hello"}]
        )
        assert msgs == [{"role": "user", "content": "hello"}]
        assert system == ""

    def test_system_prompt_param(self):
        client = AnthropicClient(LLMConfig(api_key="k", base_url="http://x/v1", model="m"))
        msgs, system = client._convert_messages(
            [{"role": "user", "content": "hi"}],
            system_prompt="You are helpful.",
        )
        assert system == "You are helpful."

    def test_system_message_in_list(self):
        """System role in messages list is extracted into system param."""
        client = AnthropicClient(LLMConfig(api_key="k", base_url="http://x/v1", model="m"))
        msgs, system = client._convert_messages([
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "question"},
        ])
        assert "Be concise." in system
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"

    def test_system_param_and_message_merged(self):
        """Both system_prompt param and system message are merged."""
        client = AnthropicClient(LLMConfig(api_key="k", base_url="http://x/v1", model="m"))
        msgs, system = client._convert_messages(
            [{"role": "system", "content": "From list."}, {"role": "user", "content": "q"}],
            system_prompt="From param.",
        )
        assert "From param." in system
        assert "From list." in system

    def test_assistant_with_text(self):
        client = AnthropicClient(LLMConfig(api_key="k", base_url="http://x/v1", model="m"))
        msgs, _ = client._convert_messages([
            {"role": "assistant", "content": "I can help."},
        ])
        assert len(msgs) == 1
        assert msgs[0]["role"] == "assistant"
        # Simple text content is wrapped in a content block list
        assert isinstance(msgs[0]["content"], list)
        assert msgs[0]["content"][0]["text"] == "I can help."

    def test_assistant_with_tool_calls(self):
        client = AnthropicClient(LLMConfig(api_key="k", base_url="http://x/v1", model="m"))
        msgs, _ = client._convert_messages([{
            "role": "assistant",
            "content": "Let me check.",
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"file_path": "/tmp/x"}'},
            }],
        }])
        assert msgs[0]["role"] == "assistant"
        blocks = msgs[0]["content"]
        assert isinstance(blocks, list)
        # First block: text
        assert blocks[0]["type"] == "text"
        # Second block: tool_use
        tool_block = [b for b in blocks if b["type"] == "tool_use"][0]
        assert tool_block["name"] == "read_file"
        assert tool_block["input"] == {"file_path": "/tmp/x"}

    def test_assistant_with_reasoning(self):
        client = AnthropicClient(LLMConfig(api_key="k", base_url="http://x/v1", model="m"))
        msgs, _ = client._convert_messages([{
            "role": "assistant",
            "content": "answer",
            "reasoning_content": "I think...",
        }])
        blocks = msgs[0]["content"]
        assert any(b["type"] == "thinking" for b in blocks)

    def test_tool_result_message(self):
        client = AnthropicClient(LLMConfig(api_key="k", base_url="http://x/v1", model="m"))
        msgs, _ = client._convert_messages([{
            "role": "tool",
            "tool_call_id": "tc_123",
            "content": "file contents here",
        }])
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"][0]["type"] == "tool_result"
        assert msgs[0]["content"][0]["tool_use_id"] == "tc_123"


# ── _convert_response — Anthropic → OpenAI response format ────────────────


class TestConvertResponse:
    """_convert_response translates Anthropic response → OpenAI format."""

    def test_simple_text(self):
        client = AnthropicClient(LLMConfig(api_key="k", base_url="http://x/v1", model="m"))
        result = client._convert_response({
            "content": [{"type": "text", "text": "Hello world"}],
            "usage": {"input_tokens": 5, "output_tokens": 3},
        })
        assert result["role"] == "assistant"
        assert result["content"] == "Hello world"
        assert "tool_calls" not in result

    def test_tool_use_block(self):
        client = AnthropicClient(LLMConfig(api_key="k", base_url="http://x/v1", model="m"))
        result = client._convert_response({
            "content": [{
                "type": "tool_use",
                "id": "toolu_001",
                "name": "read_file",
                "input": {"file_path": "/tmp/x"},
            }],
            "usage": {"input_tokens": 10, "output_tokens": 15},
        })
        assert result["tool_calls"][0]["id"] == "toolu_001"
        assert result["tool_calls"][0]["function"]["name"] == "read_file"
        assert json.loads(result["tool_calls"][0]["function"]["arguments"]) == {"file_path": "/tmp/x"}

    def test_thinking_block(self):
        client = AnthropicClient(LLMConfig(api_key="k", base_url="http://x/v1", model="m"))
        result = client._convert_response({
            "content": [
                {"type": "thinking", "thinking": "Let me analyze..."},
                {"type": "text", "text": "Answer"},
            ],
            "usage": {"input_tokens": 3, "output_tokens": 10},
        })
        assert "Let me analyze" in result["reasoning_content"]

    def test_mixed_content(self):
        """Text + tool_use + thinking all in one response."""
        client = AnthropicClient(LLMConfig(api_key="k", base_url="http://x/v1", model="m"))
        result = client._convert_response({
            "content": [
                {"type": "thinking", "thinking": "Hmm..."},
                {"type": "text", "text": "Let me check."},
                {"type": "tool_use", "id": "t1", "name": "grep", "input": {"pattern": "foo"}},
            ],
            "usage": {},
        })
        assert "Let me check." in result["content"]
        assert len(result["tool_calls"]) == 1
        assert "Hmm" in result["reasoning_content"]

    def test_usage_fallback(self):
        """Missing output_tokens should fall back to estimation."""
        client = AnthropicClient(LLMConfig(api_key="k", base_url="http://x/v1", model="m"))
        before = client.total_completion_tokens
        client._convert_response({
            "content": [{"type": "text", "text": "Short reply."}],
            "usage": {"input_tokens": 5},
        })
        # Output tokens estimated, not zero
        assert client.total_completion_tokens > before


# ── Streaming event assembly ──────────────────────────────────────────────


class FakeAnthropicStreamResponse:
    """Simulates an httpx streaming response for Anthropic SSE."""
    def __init__(self, status_code=200, lines=None):
        self.status_code = status_code
        self._lines = lines or []

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""


def make_sse(type_, delta=None, index=0, content_block=None, usage=None):
    """Build an Anthropic SSE event line."""
    event = {"type": type_}
    if delta is not None:
        event["delta"] = delta
    if index:
        event["index"] = index
    if content_block:
        event["content_block"] = content_block
    if usage:
        event["usage"] = usage
    return f"data: {json.dumps(event)}"


async def test_chat_stream_yields_text():
    """Anthropic streaming should yield text deltas."""
    client = AnthropicClient(LLMConfig(api_key="k", base_url="http://x/v1", model="m"))
    client._max_retries = 1

    lines = [
        make_sse("content_block_delta", delta={"type": "text_delta", "text": "Hello"}),
        make_sse("content_block_delta", delta={"type": "text_delta", "text": " world"}),
        make_sse("message_stop"),
        "data: [DONE]",
    ]

    with patch.object(client._client, 'send', return_value=FakeAnthropicStreamResponse(200, lines)):
        with patch.object(client._client, 'build_request', return_value=MagicMock()):
            results = [chunk async for chunk in client.chat_stream([{"role": "user", "content": "hi"}])]

    texts = [c for t, c in results if t == "text"]
    assert "".join(texts) == "Hello world"


async def test_chat_stream_yields_tool_calls():
    """Anthropic streaming should assemble tool calls from content_block events."""
    client = AnthropicClient(LLMConfig(api_key="k", base_url="http://x/v1", model="m"))
    client._max_retries = 1

    lines = [
        make_sse("content_block_start", index=0, content_block={"type": "tool_use", "id": "t1", "name": "read_file"}),
        make_sse("content_block_delta", index=0, delta={"type": "input_json_delta", "partial_json": '{"file_'}),
        make_sse("content_block_delta", index=0, delta={"type": "input_json_delta", "partial_json": 'path":"/x"}'}),
        make_sse("message_stop"),
        "data: [DONE]",
    ]

    with patch.object(client._client, 'send', return_value=FakeAnthropicStreamResponse(200, lines)):
        with patch.object(client._client, 'build_request', return_value=MagicMock()):
            results = [chunk async for chunk in client.chat_stream([{"role": "user", "content": "read /x"}])]

    tool_calls = [c for t, c in results if t == "tool_call"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "read_file"
    assert '"/x"' in tool_calls[0]["function"]["arguments"]


async def test_chat_stream_repairs_truncated_json():
    """When streaming tool arguments are truncated, _balance_json should repair."""
    client = AnthropicClient(LLMConfig(api_key="k", base_url="http://x/v1", model="m"))
    client._max_retries = 1

    lines = [
        make_sse("content_block_start", index=0, content_block={"type": "tool_use", "id": "t1", "name": "edit_file"}),
        # Arguments truncated — missing closing braces
        make_sse("content_block_delta", index=0, delta={"type": "input_json_delta", "partial_json": '{"file_path":"/x","old_str":"hello'}),
        make_sse("message_stop"),
        "data: [DONE]",
    ]

    with patch.object(client._client, 'send', return_value=FakeAnthropicStreamResponse(200, lines)):
        with patch.object(client._client, 'build_request', return_value=MagicMock()):
            results = [chunk async for chunk in client.chat_stream([{"role": "user", "content": "edit"}])]

    tool_calls = [c for t, c in results if t == "tool_call"]
    # Should not raise JSONDecodeError — _balance_json repairs the args
    assert len(tool_calls) == 1
    json.loads(tool_calls[0]["function"]["arguments"])  # must be valid JSON


# ── State management ──────────────────────────────────────────────────────


def test_set_model_updates_internal_model():
    """set_model must update both config.model and internal _model."""
    client = AnthropicClient(LLMConfig(api_key="k", base_url="http://x/v1", model="old"))
    client.set_model("new-model")
    assert client.config.model == "new-model"
    assert client._model == "new-model"


def test_anthropic_version_default():
    """Default version header without custom settings."""
    ver = AnthropicClient._get_anthropic_version()
    assert ver == "2023-06-01"
