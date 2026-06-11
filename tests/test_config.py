"""
Tests for config — validation, defaults, environment loading.
"""

import os
from unittest.mock import patch
import pytest
from ata_coder.config import LLMConfig, AgentConfig, AppConfig, get_config


class TestLLMConfig:
    """LLMConfig validation and defaults."""

    def test_default_validation_errors_no_env(self):
        """Without env vars, LLMConfig should report missing api_key and model."""
        with patch.dict(os.environ, {}, clear=True):
            config = LLMConfig(api_key="", model="")
            errors = config.validate()
            assert len(errors) >= 2
            assert any("OPENAI_API_KEY" in e for e in errors)
            assert any("OPENAI_MODEL" in e for e in errors)

    def test_validation_passes_with_api_key_and_model(self):
        """With api_key and model set, validate() should return empty list."""
        config = LLMConfig(api_key="sk-test123", model="gpt-4o")
        errors = config.validate()
        assert errors == []

    def test_validation_needs_api_key(self):
        """Missing api_key should produce a validation error."""
        config = LLMConfig(api_key="", model="gpt-4o")
        errors = config.validate()
        assert any("OPENAI_API_KEY" in e for e in errors)

    def test_validation_needs_model(self):
        """Missing model should produce a validation error."""
        config = LLMConfig(api_key="sk-test123", model="")
        errors = config.validate()
        assert any("OPENAI_MODEL" in e for e in errors)

    def test_default_temperature(self):
        """Default temperature should be 0.1."""
        with patch.dict(os.environ, {}, clear=True):
            config = LLMConfig(api_key="sk-test", model="gpt-4o")
            assert config.temperature == 0.1

    def test_temperature_from_env(self):
        """Temperature should be overridable via env var."""
        with patch.dict(os.environ, {"TEMPERATURE": "0.7"}, clear=True):
            config = LLMConfig(api_key="sk-test", model="gpt-4o")
            assert config.temperature == 0.7

    def test_max_tokens_default(self):
        """Default max_tokens should be 16384."""
        with patch.dict(os.environ, {}, clear=True):
            config = LLMConfig(api_key="sk-test", model="gpt-4o")
            assert config.max_tokens == 16384

    def test_max_tokens_from_env(self):
        """max_tokens should be overridable via env var."""
        with patch.dict(os.environ, {"MAX_OUTPUT_TOKENS": "8192"}, clear=True):
            config = LLMConfig(api_key="sk-test", model="gpt-4o")
            assert config.max_tokens == 8192

    def test_thinking_strength_from_env(self):
        """THINKING_STRENGTH env var should populate thinking_strength."""
        with patch.dict(os.environ, {"THINKING_STRENGTH": "high"}, clear=True):
            config = LLMConfig(api_key="sk-test", model="gpt-4o")
            assert config.thinking_strength == "high"

    def test_empty_thinking_strength(self):
        """Empty thinking_strength should be fine (disabled)."""
        with patch.dict(os.environ, {"THINKING_STRENGTH": ""}, clear=True):
            config = LLMConfig(api_key="sk-test", model="gpt-4o")
            assert config.thinking_strength == ""


class TestAgentConfig:
    """AgentConfig defaults and env overrides."""

    def test_default_max_tool_calls(self):
        """Default max_tool_calls should be 999."""
        with patch.dict(os.environ, {}, clear=True):
            config = AgentConfig()
            assert config.max_tool_calls == 999

    def test_max_tool_calls_from_env(self):
        """MAX_TOOL_CALLS env var should override default."""
        with patch.dict(os.environ, {"MAX_TOOL_CALLS": "50"}, clear=True):
            config = AgentConfig()
            assert config.max_tool_calls == 50

    def test_allowed_commands_includes_python(self):
        """Default allowed_commands should include python."""
        config = AgentConfig()
        assert "python" in config.allowed_commands

    def test_blocked_commands_includes_rm_rf(self):
        """Default blocked_commands should include rm -rf /."""
        config = AgentConfig()
        assert any("rm -rf /" in cmd for cmd in config.blocked_commands)


class TestAppConfig:
    """AppConfig top-level config loading."""

    def test_app_config_has_llm_and_agent(self):
        """AppConfig should contain both LLMConfig and AgentConfig."""
        config = AppConfig()
        assert isinstance(config.llm, LLMConfig)
        assert isinstance(config.agent, AgentConfig)

    @patch.dict(os.environ, {}, clear=True)
    def test_load_returns_app_config(self):
        """AppConfig.load() should return an AppConfig instance."""
        config = AppConfig.load()
        assert isinstance(config, AppConfig)

    def test_get_config_singleton(self):
        """get_config() should return the same instance on repeated calls."""
        with patch.dict(os.environ, {}, clear=True):
            c1 = get_config()
            c2 = get_config()
            assert c1 is c2
