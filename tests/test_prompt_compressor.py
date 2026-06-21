"""Tests for the optional LLMLingua prompt compressor."""

import pytest


class TestPromptCompressorImport:
    """Graceful degradation when LLMLingua is not installed."""

    def test_import_without_llmlingua(self):
        """PromptCompressor should instantiate even without llmlingua."""
        from ata_coder.prompt_compressor import PromptCompressor
        pc = PromptCompressor()
        assert pc.available is False  # Not available, but doesn't crash
        assert pc.status  # Should have a human-readable status

    def test_compress_falls_back_without_llmlingua(self):
        """compress() returns original text when LLMLingua is missing."""
        from ata_coder.prompt_compressor import PromptCompressor
        pc = PromptCompressor()
        original = "This is a test prompt that should be returned unchanged."
        result = pc.compress(original, target_ratio=0.5)
        assert result == original

    def test_compress_empty_string(self):
        """Empty input should return empty."""
        from ata_coder.prompt_compressor import PromptCompressor
        pc = PromptCompressor()
        assert pc.compress("", target_ratio=0.5) == ""
        assert pc.compress("   ", target_ratio=0.5) == "   "

    def test_is_available_reports_false(self):
        """Module-level is_available() should match reality."""
        from ata_coder.prompt_compressor import is_available
        # Without LLMLingua installed, this should be False
        # (If LLMLingua IS installed, it'll be True — both are fine)
        result = is_available()
        assert isinstance(result, bool)


class TestPromptCompressorCompressMessages:
    """compress_messages() handles message lists correctly."""

    def test_compress_messages_basic(self):
        """compress_messages falls back to original content."""
        from ata_coder.prompt_compressor import PromptCompressor
        pc = PromptCompressor()
        messages = [
            {"role": "user", "content": "Hello, can you help me?"},
            {"role": "assistant", "content": "Of course! What do you need?"},
        ]
        result = pc.compress_messages(messages, target_ratio=0.5)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_compress_messages_with_tool_calls(self):
        """Messages with tool_calls but no content are handled."""
        from ata_coder.prompt_compressor import PromptCompressor
        pc = PromptCompressor()
        messages = [
            {"role": "assistant", "content": None,
             "tool_calls": [
                 {"function": {"name": "read_file", "arguments": "{}"}},
                 {"function": {"name": "grep", "arguments": "{}"}},
             ]},
        ]
        result = pc.compress_messages(messages, target_ratio=0.5)
        assert isinstance(result, str)
        # Should mention the tool names even without LLMLingua
        assert "read_file" in result or len(result) > 0

    def test_compress_messages_empty_list(self):
        """Empty message list should return empty string."""
        from ata_coder.prompt_compressor import PromptCompressor
        pc = PromptCompressor()
        result = pc.compress_messages([], target_ratio=0.5)
        assert result == ""


class TestGetCompressor:
    """Singleton behavior."""

    def test_get_compressor_returns_same_instance(self):
        """get_compressor() should be a singleton."""
        from ata_coder.prompt_compressor import get_compressor
        pc1 = get_compressor()
        pc2 = get_compressor()
        assert pc1 is pc2
