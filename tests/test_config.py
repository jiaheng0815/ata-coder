"""
Tests for config — validation, defaults, environment loading.
"""

import os
from unittest.mock import patch, MagicMock
from ata_coder.config import LLMConfig, AgentConfig, AppConfig, get_config


def _mock_settings_empty_env():
    """Return a mock Settings that returns empty strings for env section lookups.

    This prevents real settings.json values from leaking into tests that
    verify default fallback behaviour.
    """
    mock = MagicMock()
    mock.api_key = ""
    mock.api_base_url = "https://api.deepseek.com"
    mock.default_model = "gpt-4o"
    mock.max_output_tokens = 16384
    mock.effort_level = ""
    mock.model_opus = "gpt-4o"
    mock.model_sonnet = "gpt-4o"
    mock.model_haiku = "gpt-4o"
    mock.model_subagent = "gpt-4o"
    # AgentConfig defaults
    mock.max_tool_calls = 999
    mock.max_context_tokens = 1_000_000
    mock.effective_context_tokens = 200_000
    mock.max_message_output_chars = 8_000
    mock.workspace_dir = "."
    mock.extension_dirs = []
    mock.max_sub_agents = 5
    mock.sub_agent_timeout = 300.0
    mock.temperature = 0.1
    return mock


class TestLLMConfig:
    """LLMConfig validation and defaults."""

    def test_default_validation_errors_no_env(self):
        """Without env vars, LLMConfig should report missing api_key and model."""
        with patch.dict(os.environ, {}, clear=True):
            config = LLMConfig(api_key="", model="")
            errors = config.validate()
            assert len(errors) >= 2
            assert any("API key is not set" in e for e in errors)
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
        assert any("API key is not set" in e for e in errors)

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

    def test_temperature_from_settings(self):
        """Temperature from settings is used when available."""
        settings = _mock_settings_empty_env()
        settings.temperature = 0.7
        with patch("ata_coder.settings.get_settings", return_value=settings):
            config = LLMConfig(api_key="sk-test", model="gpt-4o")
            assert config.temperature == 0.7

    def test_max_tokens_default(self):
        """Default max_tokens should be 16384 when no env var or settings env section."""
        with patch.dict(os.environ, {}, clear=True), \
             patch("ata_coder.settings.get_settings", return_value=_mock_settings_empty_env()):
            config = LLMConfig(api_key="sk-test", model="gpt-4o")
            assert config.max_tokens == 16384

    def test_max_tokens_from_settings(self):
        """max_tokens from settings should override default."""
        settings = _mock_settings_empty_env()
        settings.max_output_tokens = 8192
        with patch("ata_coder.settings.get_settings", return_value=settings):
            config = LLMConfig(api_key="sk-test", model="gpt-4o")
            assert config.max_tokens == 8192

    def test_thinking_strength_from_settings(self):
        """thinking_strength from settings should be used."""
        settings = _mock_settings_empty_env()
        settings.effort_level = "high"
        with patch("ata_coder.settings.get_settings", return_value=settings):
            config = LLMConfig(api_key="sk-test", model="gpt-4o")
            assert config.thinking_strength == "high"

    def test_empty_thinking_strength(self):
        """Empty thinking_strength should be fine (disabled)."""
        with patch.dict(os.environ, {"THINKING_STRENGTH": ""}, clear=True), \
             patch("ata_coder.settings.get_settings", return_value=_mock_settings_empty_env()):
            config = LLMConfig(api_key="sk-test", model="gpt-4o")
            assert config.thinking_strength == ""


class TestAgentConfig:
    """AgentConfig defaults and env overrides."""

    def test_default_max_tool_calls(self):
        """Default max_tool_calls should be 999."""
        settings = _mock_settings_empty_env()
        with patch("ata_coder.settings.get_settings", return_value=settings):
            config = AgentConfig()
            assert config.max_tool_calls == 999

    def test_max_tool_calls_from_settings(self):
        """max_tool_calls from settings should override default."""
        settings = _mock_settings_empty_env()
        settings.max_tool_calls = 50
        with patch("ata_coder.settings.get_settings", return_value=settings):
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
