"""
Unit tests for utils — deep_merge_dict, brief_args, enhance_api_error,
and sanitize_surrogates edge cases not covered elsewhere.
"""
import pytest
from ata_coder.utils import (
    deep_merge_dict,
    brief_args,
    enhance_api_error,
    sanitize_surrogates,
)


# ── deep_merge_dict ────────────────────────────────────────────────────────


class TestDeepMergeDict:
    """deep_merge_dict is critical for Settings config merging."""

    def test_shallow_merge(self):
        base = {"a": 1}
        override = {"b": 2}
        result = deep_merge_dict(base, override)
        assert result == {"a": 1, "b": 2}

    def test_override_scalar(self):
        base = {"a": 1}
        override = {"a": 99}
        result = deep_merge_dict(base, override)
        assert result["a"] == 99

    def test_nested_dict_merged(self):
        base = {"db": {"host": "localhost", "port": 5432}}
        override = {"db": {"host": "prod.example.com"}}
        result = deep_merge_dict(base, override)
        assert result["db"]["host"] == "prod.example.com"
        assert result["db"]["port"] == 5432  # preserved from base

    def test_list_replaced_not_merged(self):
        """Lists are replaced wholesale, not appended (user's explicit choice)."""
        base = {"allowed": ["a", "b", "c"]}
        override = {"allowed": ["x", "y"]}
        result = deep_merge_dict(base, override)
        assert result["allowed"] == ["x", "y"]

    def test_new_key_added(self):
        base = {"a": 1}
        override = {"b": {"nested": True}}
        result = deep_merge_dict(base, override)
        assert result["b"] == {"nested": True}
        assert result["a"] == 1

    def test_empty_override(self):
        base = {"a": 1, "b": 2}
        result = deep_merge_dict(base, {})
        assert result == base

    def test_empty_base(self):
        result = deep_merge_dict({}, {"a": 1})
        assert result == {"a": 1}

    def test_deeply_nested(self):
        base = {"level1": {"level2": {"level3": {"key": "old"}}}}
        override = {"level1": {"level2": {"level3": {"key": "new"}}}}
        result = deep_merge_dict(base, override)
        assert result["level1"]["level2"]["level3"]["key"] == "new"

    def test_base_not_mutated(self):
        base = {"a": 1, "nested": {"x": 1}}
        override = {"nested": {"y": 2}}
        result = deep_merge_dict(base, override)
        assert base["nested"] == {"x": 1}  # original unchanged
        assert result["nested"] == {"x": 1, "y": 2}


# ── brief_args ─────────────────────────────────────────────────────────────


class TestBriefArgs:
    """brief_args formats tool-call arguments for compact display."""

    def test_empty_dict(self):
        assert brief_args({}) == ""

    def test_none(self):
        assert brief_args(None) == ""

    def test_simple_args(self):
        result = brief_args({"file_path": "/tmp/x.py", "content": "hello"})
        assert 'file_path="/tmp/x.py"' in result
        assert 'content="hello"' in result

    def test_long_string_truncated(self):
        """Strings longer than max_str_len are truncated."""
        long_val = "x" * 200
        result = brief_args({"key": long_val}, max_str_len=50)
        assert len(result) < len(long_val) + 10
        assert "…" in result

    def test_non_string_values(self):
        result = brief_args({"count": 42, "enabled": True})
        assert "count=42" in result
        assert "enabled=True" in result


# ── enhance_api_error ──────────────────────────────────────────────────────


class TestEnhanceApiError:
    """enhance_api_error adds troubleshooting hints to API errors."""

    def test_connection_error_hint(self):
        result = enhance_api_error(502, "connection refused", "http://api.example.com/v1")
        assert "http://api.example.com/v1" in result or "connectivity" in result.lower()

    def test_context_length_hint(self):
        result = enhance_api_error(400, "context length exceeded")
        assert "smaller steps" in result or "/compact" in result


# ── sanitize_surrogates additional edge cases ──────────────────────────────


class TestSanitizeSurrogatesEdgeCases:
    """Additional edge cases beyond test_llm_client coverage."""

    def test_valid_unicode_preserved(self):
        """All valid Unicode (including emoji and CJK) must survive intact."""
        original = "Hello 世界 🌍 — em-dash and © symbol"
        result = sanitize_surrogates(original)
        assert result == original

    def test_surrogate_in_dict_key(self):
        """Lone surrogates in dictionary KEYS are also sanitized."""
        obj = {"key\ud800": "value"}
        result = sanitize_surrogates(obj)
        assert "\ud800" not in list(result.keys())[0]

    def test_boolean_and_none_preserved(self):
        assert sanitize_surrogates(True) is True
        assert sanitize_surrogates(False) is False
        assert sanitize_surrogates(None) is None

    def test_int_and_float_preserved(self):
        assert sanitize_surrogates(0) == 0
        assert sanitize_surrogates(-1) == -1
        assert sanitize_surrogates(3.14) == 3.14

    def test_empty_structures(self):
        assert sanitize_surrogates({}) == {}
        assert sanitize_surrogates([]) == []
        assert sanitize_surrogates("") == ""
