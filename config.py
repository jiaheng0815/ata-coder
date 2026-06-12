"""
Configuration management for ATA Coder.
Loads from environment variables with sensible defaults.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

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

def _ensure_env():
    global _env_loaded
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
        default_factory=lambda: os.getenv("OPENAI_MODEL", "") or _settings_default_model()
    )
    temperature: float = field(
        default_factory=lambda: float(os.getenv("TEMPERATURE", "0.1"))
    )
    max_tokens: int = field(
        default_factory=lambda: int(os.getenv("MAX_OUTPUT_TOKENS", "16384"))
    )
    thinking_strength: str = field(
        default_factory=lambda: os.getenv("THINKING_STRENGTH", "")
    )  # "" = default, "off" = explicitly disabled, "low"|"medium"|"high"|"xhigh"|"max"

    def validate(self) -> list[str]:
        """Validate configuration, returns list of errors."""
        errors = []
        if not self.api_key:
            errors.append(
                "OPENAI_API_KEY is not set. "
                "Set it in .env file or as environment variable."
            )
        if not self.model:
            errors.append("OPENAI_MODEL is not set.")
        return errors


@dataclass
class AgentConfig:
    """Configuration for the agent behavior."""

    max_tool_calls: int = field(
        default_factory=lambda: int(os.getenv("MAX_TOOL_CALLS", "999"))
    )
    max_context_tokens: int = field(
        default_factory=lambda: int(
            os.getenv("MAX_CONTEXT_TOKENS", "1000000")  # 1M for DeepSeek/Claude large-context models
        )
    )
    effective_context_tokens: int = field(
        default_factory=lambda: int(
            os.getenv("EFFECTIVE_CONTEXT_TOKENS", "200000")
            # Models claim 1M but attention quality drops sharply after ~200k.
            # We compact BEFORE this threshold to keep the model in its sweet spot.
            # Set higher if your model genuinely handles long context well.
        )
    )
    max_message_output_chars: int = field(
        default_factory=lambda: int(
            os.getenv("MAX_MESSAGE_OUTPUT_CHARS", "8000")
            # Tool results stored in message history are capped at this size.
            # The full result is still available during execution — this only
            # limits what gets sent to the LLM on subsequent turns.
        )
    )
    workspace_dir: str = field(
        default_factory=lambda: os.getenv("WORKSPACE_DIR", str(Path.cwd()))
    )

    # Extension & sub-agent settings
    extension_dirs: list[str] = field(
        default_factory=lambda: [
            d.strip() for d in os.getenv("EXTENSION_DIRS", "").split(",") if d.strip()
        ]
    )
    max_sub_agents: int = field(
        default_factory=lambda: int(os.getenv("MAX_SUB_AGENTS", "5"))
    )
    sub_agent_timeout: float = field(
        default_factory=lambda: float(os.getenv("SUB_AGENT_TIMEOUT", "300.0"))
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
    effort: str = field(default_factory=lambda: os.getenv("ATA_EFFORT", "medium"))

    @classmethod
    def load(cls) -> "AppConfig":
        """Load and validate all configuration (errors logged by caller)."""
        _ensure_env()
        config = cls()
        return config


# Lazy-loaded global config
_config: AppConfig | None = None


def get_config() -> AppConfig:
    """Get or create the global config instance (lazy load)."""
    global _config
    if _config is None:
        _config = AppConfig.load()
    return _config


def _settings_api_key() -> str:
    """API key: env var > settings.json > empty."""
    env_val = os.getenv("OPENAI_API_KEY", "")
    if env_val:
        return env_val
    try:
        from .settings import get_settings
        return get_settings().api_key
    except Exception:
        return ""


def _settings_base_url() -> str:
    """Base URL: env var > settings.json > hardcoded default."""
    env_val = os.getenv("OPENAI_BASE_URL", "")
    if env_val:
        return env_val
    try:
        from .settings import get_settings
        return get_settings().api_base_url
    except Exception:
        return "https://api.openai.com/v1"


def _settings_default_model() -> str:
    """Get the default model from settings.json, with lazy import to avoid circular deps."""
    try:
        from .settings import get_settings
        return get_settings().default_model
    except Exception:
        return "gpt-4o"


# Use get_config() to obtain the global AppConfig instance.
# Lazy-loading is handled inside get_config() itself.

