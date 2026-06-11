"""
Model routing — AI-driven complexity classification + model selection.

Flow:
  1. User sends a task
  2. Cheap model (haiku/subagent) classifies: simple or complex?
  3. Simple → keep cheap model   |   Complex → switch to powerful model

No hardcoded keywords — the cheap model itself judges complexity.
"""

import logging
from typing import Any

from .settings import get_settings, Settings

logger = logging.getLogger(__name__)


def get_subagent_model(settings: Settings | None = None) -> str:
    """Get the model name for classification/subagent tasks (cheaper/faster)."""
    if settings is None:
        settings = get_settings()
    return settings.model_subagent


def get_complex_model(settings: Settings | None = None) -> str:
    """Get the model name for complex tasks (powerful)."""
    if settings is None:
        settings = get_settings()
    return settings.model_opus


def resolve_model(complexity: str, settings: Settings | None = None) -> str:
    """
    Map a complexity label to a model name.

    Args:
        complexity: 'simple' | 'complex' | 'normal' | 'explicit'

    Returns:
        Model name string
    """
    if settings is None:
        settings = get_settings()

    mapping = {
        "simple": settings.model_haiku,
        "complex": settings.model_opus,
        "normal": settings.default_model,
        "explicit": settings.default_model,
    }
    return mapping.get(complexity, settings.default_model)


def get_model_info(settings: Settings | None = None) -> dict[str, Any]:
    """Get all model configuration info for display."""
    if settings is None:
        settings = get_settings()
    return {
        "default": settings.default_model,
        "opus": settings.model_opus,
        "sonnet": settings.model_sonnet,
        "haiku": settings.model_haiku,
        "subagent": settings.model_subagent,
    }
