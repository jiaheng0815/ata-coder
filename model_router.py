"""
Model routing — length shortcuts + AI-driven complexity classification + model selection.

Flow:
  1. User sends a task
  2. shortcut_classify() tries a cheap length-based heuristic
  3. If ambiguous, the cheap model classifies: simple or complex?
  4. Simple → keep cheap model   |   Complex → switch to powerful model

No hardcoded keywords — the cheap model itself judges complexity for
medium-length tasks.
"""

import logging
from typing import Any

from .settings import get_settings, Settings

logger = logging.getLogger(__name__)


class ModelRouter:
    """Consolidated complexity classification + model name resolution.

    Pulls configuration from the global Settings singleton so callers
    don't need to thread settings through every call site.
    """

    def __init__(self, settings: Settings | None = None):
        self._settings = settings or get_settings()

    # ── Complexity classification ──────────────────────────────────────────

    def classify_shortcut(self, task: str) -> str | None:
        """
        Quick length-based shortcut — skip AI classify for obvious cases.
        Returns 'simple', 'complex', or None (None = need AI classify).

        Moved here from Settings.shortcut_classify to keep classification
        logic in one place.
        """
        if not self._settings.get("complexity", "auto_detect", default=True):
            return "normal"

        task_len = len(task.strip())
        simple_max = self._settings.get("complexity", "simple_max_chars", default=60)
        complex_min = self._settings.get("complexity", "complex_min_chars", default=500)

        if task_len <= simple_max:
            return "simple"
        if task_len >= complex_min:
            return "complex"
        return None  # middle ground → let AI decide

    # ── Model resolution ───────────────────────────────────────────────────

    def resolve(self, complexity: str) -> str:
        """Map a complexity label to a model name."""
        mapping = {
            "simple": self._settings.model_haiku,
            "complex": self._settings.model_opus,
            "normal": self._settings.default_model,
            "explicit": self._settings.default_model,
        }
        return mapping.get(complexity, self._settings.default_model)

    @property
    def subagent_model(self) -> str:
        return self._settings.model_subagent

    @property
    def complex_model(self) -> str:
        return self._settings.model_opus

    @property
    def info(self) -> dict[str, Any]:
        return {
            "default": self._settings.default_model,
            "opus": self._settings.model_opus,
            "sonnet": self._settings.model_sonnet,
            "haiku": self._settings.model_haiku,
            "subagent": self._settings.model_subagent,
        }


# ── Module-level convenience functions (keep backward compat) ──────────────

def get_subagent_model(settings: Settings | None = None) -> str:
    """Get the model name for classification/subagent tasks (cheaper/faster)."""
    return ModelRouter(settings).subagent_model


def get_complex_model(settings: Settings | None = None) -> str:
    """Get the model name for complex tasks (powerful)."""
    return ModelRouter(settings).complex_model


def resolve_model(complexity: str, settings: Settings | None = None) -> str:
    """Map a complexity label to a model name. Delegates to ModelRouter."""
    return ModelRouter(settings).resolve(complexity)


def get_model_info(settings: Settings | None = None) -> dict[str, Any]:
    """Get all model configuration info for display. Delegates to ModelRouter."""
    return ModelRouter(settings).info
