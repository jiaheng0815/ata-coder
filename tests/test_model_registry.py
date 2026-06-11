"""
Tests for model_registry — model info lookup, URL building, cost estimation.
"""

import pytest
from ata_coder.model_registry import (
    ModelInfo,
    get_model_info,
    get_model_cost,
    estimate_cost,
    build_api_url,
    build_models_url,
    MODEL_REGISTRY,
)


class TestModelInfo:
    """ModelInfo dataclass behavior."""

    def test_known_model_gpt4o(self):
        """GPT-4o should have expected pricing."""
        info = get_model_info("gpt-4o")
        assert info.input_price_per_1m == 2.50
        assert info.output_price_per_1m == 10.00
        assert info.provider == "openai"

    def test_known_model_deepseek(self):
        """DeepSeek Chat should have expected pricing."""
        info = get_model_info("deepseek-chat")
        assert info.input_price_per_1m == 0.14
        assert info.provider == "deepseek"

    def test_unknown_model_returns_fallback(self):
        """Unknown models should return fallback pricing."""
        info = get_model_info("nonexistent-model-v42")
        assert info.input_price_per_1m == 1.00
        assert info.output_price_per_1m == 5.00
        assert info.provider == "unknown"

    def test_model_with_suffix_brackets(self):
        """Model IDs with [suffix] should match by clean name."""
        info = get_model_info("deepseek-v4-pro[1m]")
        assert info.input_price_per_1m == 0.14
        assert info.provider == "deepseek"

    def test_model_with_context_suffix(self):
        """Model IDs like 'gpt-4o[context]' should still match."""
        info = get_model_info("gpt-4o[context]")
        assert info.input_price_per_1m == 2.50

    def test_model_substring_match(self):
        """Model IDs containing a known key should match via substring."""
        info = get_model_info("some-prefix-deepseek-chat-v2")
        assert info.provider == "deepseek"

    def test_model_info_immutable(self):
        """ModelInfo is frozen and cannot be modified."""
        info = get_model_info("gpt-4o")
        with pytest.raises(Exception):
            info.input_price_per_1m = 999.0

    def test_all_registry_models_resolve(self):
        """Every model in the registry should resolve to itself."""
        for model_id in MODEL_REGISTRY:
            info = get_model_info(model_id)
            assert info.model_id == model_id


class TestGetModelCost:
    """get_model_cost returns (input_price, output_price)."""

    def test_cost_tuple(self):
        """get_model_cost should return a tuple of two floats."""
        inp, out = get_model_cost("gpt-4o")
        assert isinstance(inp, float)
        assert isinstance(out, float)

    def test_known_model_cost(self):
        """Known model should return its registered prices."""
        inp, out = get_model_cost("gpt-4o-mini")
        assert inp == 0.15
        assert out == 0.60


class TestEstimateCost:
    """estimate_cost calculates USD cost."""

    def test_estimate_gpt4o(self):
        """1M tokens at 70% input ratio on GPT-4o."""
        cost = estimate_cost(1_000_000, "gpt-4o")
        expected = (700_000 / 1_000_000) * 2.50 + (300_000 / 1_000_000) * 10.00
        assert cost == pytest.approx(expected)

    def test_estimate_zero_tokens(self):
        """0 tokens should cost $0."""
        cost = estimate_cost(0, "gpt-4o")
        assert cost == 0.0

    def test_estimate_custom_ratio(self):
        """Custom input_ratio should affect cost."""
        cost70 = estimate_cost(1_000_000, "gpt-4o", input_ratio=0.7)
        cost50 = estimate_cost(1_000_000, "gpt-4o", input_ratio=0.5)
        assert cost70 != cost50


class TestBuildApiUrl:
    """URL building logic."""

    def test_openai_url(self):
        """OpenAI base URL should produce /v1/chat/completions."""
        url = build_api_url("https://api.openai.com")
        assert url == "https://api.openai.com/v1/chat/completions"

    def test_deepseek_url_with_v1(self):
        """DeepSeek base URL with /v1 should preserve it."""
        url = build_api_url("https://api.deepseek.com/v1")
        assert url == "https://api.deepseek.com/v1/chat/completions"

    def test_deepseek_url_with_v2(self):
        """Base URL with /v2 should produce /v2/chat/completions."""
        url = build_api_url("https://api.deepseek.com/v2")
        assert url == "https://api.deepseek.com/v2/chat/completions"

    def test_trailing_slash(self):
        """Trailing slash on base URL should be handled gracefully."""
        url = build_api_url("https://api.openai.com/")
        assert url == "https://api.openai.com/v1/chat/completions"

    def test_custom_path(self):
        """Custom base with a path should work."""
        url = build_api_url("https://localhost:8080/v1")
        assert url == "https://localhost:8080/v1/chat/completions"

    def test_empty_endpoint_returns_base(self):
        """Empty endpoint should return just the versioned base URL."""
        url = build_api_url("https://api.openai.com", endpoint="")
        assert url == "https://api.openai.com/v1"


class TestBuildModelsUrl:
    """Models URL building."""

    def test_models_url_with_v1(self):
        """Base URL with /v1 should produce /v1/models."""
        url = build_models_url("https://api.openai.com")
        assert url == "https://api.openai.com/v1/models"

    def test_models_url_with_v2(self):
        """Base URL with /v2 should produce /v2/models."""
        url = build_models_url("https://api.deepseek.com/v2")
        assert url == "https://api.deepseek.com/v2/models"

    def test_models_url_trailing_slash(self):
        """Trailing slash should be handled."""
        url = build_models_url("https://api.openai.com/")
        assert url == "https://api.openai.com/v1/models"
