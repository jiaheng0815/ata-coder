"""
Shared display utilities — used by both the GUI and the API server.

- brief_args: truncate tool-call argument dicts for compact display
- enhance_api_error: add helpful suggestions to common API errors
- try_import_yaml: shared conditional yaml import (used by memory + skills)
"""

from typing import Any


# ── YAML availability (shared across memory.py, skills.py, etc.) ──────────

try:
    import yaml as _yaml
    HAS_YAML = True
except ImportError:
    _yaml = None  # type: ignore
    HAS_YAML = False


def try_import_yaml():
    """Return (yaml_module, True) if PyYAML is installed, else (None, False).
    """
    return _yaml, HAS_YAML


def create_llm_client(llm_config):
    """Factory: create the correct LLM client (Anthropic or OpenAI) from config.

    Returns (client, use_anthropic: bool). Used by both CoderAgent and SubAgent
    to avoid duplicating the same Anthropic-vs-OpenAI dispatch logic.
    """
    if llm_config.use_anthropic:
        from .anthropic_client import AnthropicClient  # noqa: E402 — lazy import
        return AnthropicClient(llm_config), True
    from .llm_client import LLMClient  # noqa: E402 — lazy import
    return LLMClient(llm_config), False


def brief_args(args: dict[str, Any] | None, max_str_len: int = 100) -> str:
    """Format tool-call arguments into a compact single-line summary.

    Returns an empty string for None/empty dicts.  String values longer
    than *max_str_len* are truncated with an ellipsis.

    Used by:
      gui.py   — tool-call display in the chat pane
      server.py — SSE stream event logging
    """
    if not args:
        return ""
    parts = []
    for k, v in args.items():
        if isinstance(v, str):
            s = f"{k}="
            if len(v) > max_str_len:
                s += f'"{v[:max_str_len]}…"'
            else:
                s += f'"{v}"'
            parts.append(s)
        else:
            parts.append(f"{k}={v}")
    return "  ".join(parts)


def enhance_api_error(status_code: int, error_message: str, base_url: str = "") -> str:
    """Add helpful troubleshooting suggestions to common API errors."""
    msg = error_message

    if "model" in error_message.lower() and ("not found" in error_message.lower() or "not supported" in error_message.lower()):
        msg += (
            "\n\n💡 This usually means the model name is incorrect for this provider."
            "\n   → Check your ATA_CODER_DEFAULT_MODEL in settings.json"
            "\n   → For DeepSeek: try 'deepseek-chat' or 'deepseek-v4-pro' (no [1m] suffix)"
            "\n   → Run 'ata --list-models' to see what the API reports as available"
        )
    elif "unauthorized" in error_message.lower() or status_code == 401:
        msg += (
            "\n\n🔑 Authentication failed. Check your API key:"
            "\n   → Settings: ~/.ata_coder/settings.json → env.ATA_CODER_API_KEY"
            "\n   → Or set ATA_CODER_API_KEY environment variable"
        )
    elif "rate limit" in error_message.lower() or status_code == 429:
        msg += (
            "\n\n⏳ Rate limited — the API is throttling requests."
            "\n   → Wait a moment and try again"
            "\n   → Consider switching to a model with higher rate limits"
        )
    elif "connection" in error_message.lower() or status_code in (502, 503, 504):
        msg += (
            "\n\n🌐 Server connectivity issue:"
            "\n   → Check: is the API base URL correct? → " + (base_url or "check settings.json")
            + "\n   → The server may be temporarily overloaded"
            "\n   → Try again in a few seconds"
        )
    elif "context length" in error_message.lower() or "too long" in error_message.lower():
        msg += (
            "\n\n📏 Context length exceeded:"
            "\n   → Try breaking your task into smaller steps"
            "\n   → Use /compact to shrink conversation history"
            "\n   → Increase ATA_CODER_MAX_OUTPUT_TOKENS for larger context"
        )

    return msg


# ── Surrogate sanitization ─────────────────────────────────────────────────


def sanitize_surrogates(obj: Any, _depth: int = 0, _max_depth: int = 500) -> Any:
    """Replace lone surrogates (U+D800–U+DFFF) in all strings.

    Python's ``json.dumps(ensure_ascii=False)`` can emit lone surrogates that
    are invalid UTF-8.  When httpx or a file write tries ``.encode("utf-8")``
    on the result, Python raises ``UnicodeEncodeError: surrogates not allowed``.

    This round-trip through UTF-8 with ``errors="replace"`` strips them safely.
    Depth-limited to guard against maliciously nested payloads.
    """
    if _depth > _max_depth:
        return "[truncated: max nesting depth exceeded]"
    if isinstance(obj, str):
        return obj.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(obj, dict):
        return {sanitize_surrogates(k, _depth + 1, _max_depth): sanitize_surrogates(v, _depth + 1, _max_depth) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_surrogates(v, _depth + 1, _max_depth) for v in obj]
    return obj


def deep_merge_dict(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base*.

    Dict values are recursively merged; lists and scalars are replaced
    wholesale (no append).  This is intentional — lists like
    ``allowed_commands`` represent the user's explicit choice, not a
    cumulative set.

    Moved from ``Settings._deep_merge`` to keep ``utils.py`` as the
    single home for general-purpose dictionary utilities.
    """
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = deep_merge_dict(result[k], v)
        elif k in result and isinstance(result[k], list) and isinstance(v, list):
            # List override: user's values replace defaults (explicit choice)
            result[k] = v
        else:
            result[k] = v
    return result
