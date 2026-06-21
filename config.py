"""
Configuration management for ATA Coder.
Loads from environment variables with sensible defaults.
"""

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from dotenv import load_dotenv


def _find_project_root() -> Path:
    """Find the project root directory."""
    return Path(__file__).parent.resolve()


def _load_env():
    """Load .env files: CWD first (lowest priority), then project root overrides."""
    cwd_env = Path.cwd() / ".env"
    project_env = _find_project_root() / ".env"
    # Load CWD .env as baseline
    if cwd_env.exists():
        load_dotenv(cwd_env)
    # Project-root .env takes precedence (overrides CWD values)
    if project_env.exists() and project_env != cwd_env:
        load_dotenv(project_env, override=True)


# Defer env loading to first config access, not import time
_env_loaded = False
_env_lock = threading.Lock()


def _ensure_env():
    """Thread-safe lazy env loader."""
    global _env_loaded
    if _env_loaded:
        return
    with _env_lock:
        if not _env_loaded:
            _load_env()
            _env_loaded = True


@dataclass
class LLMConfig:
    """Configuration for the OpenAI-compatible LLM client."""

    api_key: str = field(
        default_factory=lambda: _settings_api_key()
    )
    base_url: str = field(
        default_factory=lambda: _settings_base_url()
    )
    model: str = field(
        default_factory=lambda: _settings_default_model()
    )
    temperature: float = field(
        default_factory=lambda: _safe_temperature()
    )
    max_tokens: int = field(
        default_factory=lambda: _settings_max_output_tokens()
    )
    thinking_strength: str = field(
        default_factory=lambda: _settings_thinking_strength()
    )  # "" = default, "off" = explicitly disabled, "low"|"medium"|"high"|"xhigh"|"max"
    use_anthropic: bool = field(
        default_factory=lambda: _from_settings("use_anthropic", False)
    )

    def __post_init__(self):
        """Normalize model name: auto-strip [1m] suffix (DeepSeek Anthropic marker)."""
        if self.model and self.model.endswith("[1m]"):
            self.model = self.model[:-4].strip()
            logger.info(
                "Auto-stripped [1m] suffix from model: %s", self.model
            )

    def validate(self) -> list[str]:
        """Validate configuration, returns list of errors."""
        errors = []
        if not self.api_key:
            errors.append(
                "API key is not set. "
                "Set ATA_CODER_API_KEY in ~/.ata_coder/settings.json env block, "
                "or as an environment variable."
            )
        if not self.model:
            errors.append("OPENAI_MODEL is not set.")
        return errors


@dataclass
class AgentConfig:
    """Configuration for the agent behavior."""

    max_tool_calls: int = field(
        default_factory=lambda: int(_from_settings("max_tool_calls", 999))
    )
    max_context_tokens: int = field(
        default_factory=lambda: int(_from_settings("max_context_tokens", 1000000))
    )
    effective_context_tokens: int = field(
        default_factory=lambda: int(_from_settings("effective_context_tokens", 200000))
    )

    def __post_init__(self) -> None:
        """Validate context window budget ordering."""
        if self.effective_context_tokens >= self.max_context_tokens:
            logger.warning(
                "effective_context_tokens (%d) >= max_context_tokens (%d) — "
                "clamping effective to 90%% of max so compaction can still trigger.",
                self.effective_context_tokens, self.max_context_tokens,
            )
            object.__setattr__(
                self, "effective_context_tokens",
                max(10000, int(self.max_context_tokens * 0.9)),
            )
    # Compaction/context budgets (passed to ContextManager)
    recent_token_budget: int = field(
        default_factory=lambda: int(_from_settings("recent_token_budget", 80000))
    )
    compact_if_fewer_than: int = field(
        default_factory=lambda: int(_from_settings("compact_if_fewer_than", 6))
    )
    max_message_output_chars: int = field(
        default_factory=lambda: int(_from_settings("max_message_output_chars", 8000))
    )
    workspace_dir: str = field(
        default_factory=lambda: _from_settings("workspace_dir", str(Path.cwd()))
    )

    # Extension & sub-agent settings
    extension_dirs: list[str] = field(
        default_factory=lambda: _from_settings("extension_dirs", [])
    )
    max_sub_agents: int = field(
        default_factory=lambda: int(_from_settings("max_sub_agents", 5))
    )
    sub_agent_timeout: float = field(
        default_factory=lambda: float(_from_settings("sub_agent_timeout", 300.0))
    )

    # Safety settings
    allowed_commands: list[str] = field(
        default_factory=lambda: [
            "ls", "dir", "cat", "head", "tail", "wc", "find",
            "git", "python", "python3", "node", "npm", "npx",
            "pip", "poetry", "cargo", "go", "rustc", "javac", "java",
            "make", "cmake", "gcc", "g++", "clang", "clang++",
            "tsc", "eslint", "prettier", "pytest", "jest",
            "mypy", "ruff", "black", "isort",
            "echo", "mkdir", "touch", "cp", "mv", "rm",
            "cd",  # harmless — cwd is already workspace; cd in compound commands just navigates within one shell invocation
            "grep", "rg", "fd", "tree",
            "docker", "docker-compose",
            "cowsay", "fortune", "date", "whoami", "pwd",
        ]
    )

    # Blocked commands for safety
    blocked_commands: list[str] = field(
        default_factory=lambda: [
            "rm -rf /",
            "mkfs.",
            "dd if=",
            ":(){ :|:& };:",  # fork bomb
            "shutdown",
            "reboot",
            "chmod 777 /",
            "> /dev/sda",
        ]
    )


@dataclass
class AppConfig:
    """Top-level application configuration."""

    llm: LLMConfig = field(default_factory=LLMConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    effort: str = field(default_factory=lambda: (_from_settings("effort_level") or "medium"))

    @classmethod
    def load(cls) -> "AppConfig":
        """Load and validate all configuration (errors logged by caller)."""
        _ensure_env()
        config = cls()
        return config


# Module-level config — initialised lazily via get_config().
# Using a lazy pattern because AppConfig.load() references _from_settings()
# which is defined after the dataclass body in this module.
_config: AppConfig | None = None
_config_lock = threading.Lock()


def get_config() -> AppConfig:
    """Return the module-level config singleton (lazy init on first call).

    After the first call the config is cached.  Double-checked locking
    protects the lazy-init path when server.py runs under ThreadingHTTPServer
    (where multiple threads may race on the first call).
    """
    global _config
    if _config is None:
        with _config_lock:
            if _config is None:  # double-check
                _config = AppConfig.load()
    return _config


# ── Single config resolution helper ────────────────────────────────────────
# All config values come from settings.json. CLI overrides (--model, --api-key)
# are applied later via _apply_config_overrides() in main.py.
# Environment variables are intentionally NOT read — settings.json is the
# single source of truth for predictable, reproducible configuration.


def _from_settings(attr: str, default: Any = "") -> Any:
    """Read a config value from the Settings singleton.

    Lazy import to avoid circular dependency with settings.py.
    Always returns *default* when settings are unavailable (fail-safe),
    but logs a warning for non-trivial failures so they don't go unnoticed.
    """
    try:
        from .settings import get_settings
    except ImportError:
        return default
    try:
        settings = get_settings()
        return getattr(settings, attr, default)
    except AttributeError:
        # settings object exists but attribute is missing — use default
        logger = logging.getLogger(__name__)
        logger.debug("Settings property %r not found, using default %r", attr, default)
        return default
    except Exception:
        # Catch-all for unexpected error types (e.g., OSError on corrupt file,
        # TypeError from malformed data). These are logged at WARNING with full
        # traceback so they don't go unnoticed, but the system stays running.
        logger = logging.getLogger(__name__)
        logger.warning(
            "Failed to read settings.%s — using default %r. "
            "Check your settings.json for corruption.", attr, default, exc_info=True
        )
        return default


def _safe_temperature() -> float:
    """Read temperature from settings, ensuring it's a numeric type."""
    val = _from_settings("temperature", 0.1)
    return val if isinstance(val, (int, float)) else 0.1


def _settings_api_key() -> str:
    """Resolve API key with tiered fallback (OS keychain → env vars → settings.json)."""
    try:
        from .settings import resolve_api_key
        return resolve_api_key()
    except ImportError:
        return _from_settings("api_key", "")


def _settings_base_url() -> str:
    return _from_settings("api_base_url", "https://api.deepseek.com")


def _settings_default_model() -> str:
    return _from_settings("default_model", "deepseek-v4-pro")


def _settings_max_output_tokens() -> int:
    return _from_settings("max_output_tokens", 16384)


def _settings_thinking_strength() -> str:
    return _from_settings("effort_level", "")


# Use get_config() to obtain the global AppConfig instance.
# Lazy-loading is handled inside get_config() itself.

