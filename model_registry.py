"""
Shared model metadata — single source of truth for pricing, URL building,
and model info. Eliminates the duplicated price tables and URL construction
that were scattered across commands.py, repl_ui.py, main.py, and llm_client.py.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelInfo:
    """Immutable metadata for a known model."""
    model_id: str
    input_price_per_1m: float   # USD per 1M input tokens
    output_price_per_1m: float   # USD per 1M output tokens
    provider: str = ""           # "openai" | "deepseek" | "anthropic" | "local"


# ── Registry ─────────────────────────────────────────────────────────────────

MODEL_REGISTRY: dict[str, ModelInfo] = {
    "gpt-4o":              ModelInfo("gpt-4o",              2.50,  10.00, "openai"),
    "gpt-4o-mini":         ModelInfo("gpt-4o-mini",         0.15,   0.60, "openai"),
    "gpt-4-turbo":         ModelInfo("gpt-4-turbo",        10.00,  30.00, "openai"),
    "gpt-4":               ModelInfo("gpt-4",              30.00,  60.00, "openai"),
    "deepseek-chat":       ModelInfo("deepseek-chat",       0.14,   0.28, "deepseek"),
    "deepseek-coder":      ModelInfo("deepseek-coder",      0.14,   0.28, "deepseek"),
    "deepseek-v4-pro":     ModelInfo("deepseek-v4-pro",     0.14,   0.28, "deepseek"),
    "deepseek-v4-flash":   ModelInfo("deepseek-v4-flash",   0.14,   0.28, "deepseek"),
    "claude-sonnet-4-6":   ModelInfo("claude-sonnet-4-6",   3.00,  15.00, "anthropic"),
    "claude-opus-4-8":     ModelInfo("claude-opus-4-8",    15.00,  75.00, "anthropic"),
    "qwen2.5-coder-14b":   ModelInfo("qwen2.5-coder-14b",   0.00,   0.00, "local"),
}

# Fallback prices when model is not in the registry
_FALLBACK_INPUT_PRICE = 1.00
_FALLBACK_OUTPUT_PRICE = 5.00


def get_model_info(model_id: str | None) -> ModelInfo:
    """Look up a model in the registry. Returns a fallback for unknown models.

    Resolution order:
    1. Exact match (e.g. "gpt-4o" → openai)
    2. Strip bracket suffixes like ``[1m]`` / ``[context]``, then exact match
    3. Substring match — longest known key found inside *model_id* wins
       (e.g. "some-prefix-deepseek-chat-v2" → deepseek)
    """
    # Guard against None (callers may pass optional model_id)
    if model_id is None:
        return ModelInfo("unknown", _FALLBACK_INPUT_PRICE, _FALLBACK_OUTPUT_PRICE, "unknown")
    # Exact match first
    if model_id in MODEL_REGISTRY:
        return MODEL_REGISTRY[model_id]
    # Strip common suffixes that providers append: "[1m]", "[context]", etc.
    import re
    clean = re.sub(r'\[.*\]', '', model_id).strip()
    if clean in MODEL_REGISTRY:
        return MODEL_REGISTRY[clean]
    # Substring match — longest key wins (prevents "gpt-4" matching before "gpt-4o")
    for key in sorted(MODEL_REGISTRY, key=len, reverse=True):
        if key in model_id:
            return MODEL_REGISTRY[key]
    return ModelInfo(model_id, _FALLBACK_INPUT_PRICE, _FALLBACK_OUTPUT_PRICE, "unknown")


def get_model_cost(model_id: str) -> tuple[float, float]:
    """Return (input_price_per_1m, output_price_per_1m) for a model."""
    info = get_model_info(model_id)
    return info.input_price_per_1m, info.output_price_per_1m


def estimate_cost(token_count: int, model_id: str,
                  input_ratio: float = 0.7) -> float:
    """
    Estimate USD cost from a total token count.
    Assumes *input_ratio* fraction of tokens are input (default 70%).
    """
    inp_price, out_price = get_model_cost(model_id)
    input_tokens = int(token_count * input_ratio)
    output_tokens = token_count - input_tokens
    return (input_tokens / 1_000_000) * inp_price + (output_tokens / 1_000_000) * out_price


# ── URL building ─────────────────────────────────────────────────────────────

def build_api_url(base_url: str, endpoint: str = "chat/completions") -> str:
    """
    Build a complete OpenAI-compatible API URL from a base URL and endpoint.

    Normalizes the base URL:
        https://api.openai.com          → https://api.openai.com/v1/chat/completions
        https://api.deepseek.com/v1     → https://api.deepseek.com/v1/chat/completions
        https://api.deepseek.com/v2     → https://api.deepseek.com/v2/chat/completions

    Use endpoint="" to get just the versioned base, e.g. for /models listing.
    """
    import re
    base = base_url.rstrip("/")
    if not re.search(r'/v\d+', base):
        base += "/v1"
    if endpoint:
        return f"{base}/{endpoint.lstrip('/')}"
    return base


def build_models_url(base_url: str) -> str:
    """Build the /models endpoint URL from a base URL."""
    import re as _re
    base = base_url.rstrip("/")
    # Match any /vN version segment (v1, v2, v3, ...) to avoid
    # false-positives from substring matches and support future API versions.
    if _re.search(r'/v\d+', base):
        return f"{base}/models"
    return f"{base}/v1/models"


# ── Model list from API ──────────────────────────────────────────────────────

def fetch_available_models(base_url: str, api_key: str, timeout: float = 10.0) -> list[str]:
    """
    Fetch the available model list from the API's /models endpoint.
    Returns model IDs, or an empty list on failure.

    .. note::
       This uses synchronous ``httpx.get()``.  Callers inside an async
       event loop should use ``asyncio.to_thread(fetch_available_models, ...)``
       to avoid blocking the loop.
    """
    import httpx
    url = build_models_url(base_url)
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        resp = httpx.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        return [m.get("id", "") for m in data.get("data", [])]
    except Exception:
        return []
