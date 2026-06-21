"""Model routing and task complexity classification — mixin for CoderAgent."""
import logging

from .settings import get_settings

logger = logging.getLogger(__name__)


class ModelRoutingMixin:
    """Model routing based on task complexity heuristics and effort level.

    Contract (host class: ``CoderAgent``):
        Requires:
        - ``self.config`` — AppConfig instance
        Provides:
        - ``_classify_task()`` — keyword+length heuristic (no API call)
        - ``_ai_classify()`` — LLM-based classification for ambiguous cases
    """

    # ── Model routing ────────────────────────────────────────────────────

    def _route_for_task(self, task: str, explicit_model: str = "") -> None:
        """Route to the appropriate model based on task complexity + effort level."""
        if explicit_model:
            self._route_model(explicit_model)
            self._routed_complexity = "explicit"
        else:
            settings = get_settings()
            complexity = self._ai_classify(task)

            if complexity == "simple":
                routed_model = settings.model_haiku
            elif complexity == "complex":
                routed_model = settings.model_opus
            elif complexity == "normal":
                routed_model = settings.default_model
            else:
                logger.warning("Unknown complexity %r", complexity)
                routed_model = settings.default_model

            # Adjust by effort
            effort = getattr(self.config, "effort", "medium")
            if effort == "low":
                routed_model = settings.model_haiku
            elif effort in ("xhigh", "max"):
                routed_model = settings.model_opus

            self._route_model(routed_model)
            self._routed_complexity = complexity

        logger.info("Model: %s (complexity=%s)", self.current_model, self._routed_complexity)

    def _route_model(self, model: str) -> None:
        """Switch the LLM client to use a different model at runtime."""
        if self.llm.config.model == model:
            return
        logger.info("Switching model: %s → %s", self.llm.config.model, model)
        self.llm.config.model = model

    @property
    def current_model(self) -> str:
        return self.llm.config.model

    @staticmethod
    def _ai_classify(task: str) -> str:
        """
        Classify task complexity using fast heuristics (NO extra LLM call).

        Returns 'simple', 'complex', or 'normal'.

        Heuristics:
        - Very short (< 60 chars) → simple
        - Very long (> 500 chars) → complex
        - Contains complexity keywords → complex
        - Contains simple-query keywords → simple
        - Default → normal
        """
        task_lower = task.lower().strip()
        task_len = len(task)

        # ── Length-based shortcut (configurable thresholds) ──────────────
        try:
            s = get_settings()
            simple_max = s.get("complexity", "simple_max_chars", default=60)
            complex_min = s.get("complexity", "complex_min_chars", default=500)
        except (ImportError, AttributeError, KeyError):
            simple_max, complex_min = 60, 500  # fallback defaults

        if task_len <= simple_max:
            return "simple"
        if task_len >= complex_min:
            return "complex"

        # ── Complexity keywords ───────────────────────────────────────
        complex_keywords = [
            "implement", "refactor", "architecture", "migrate", "redesign",
            "optimize", "debug", "fix bug", "restructure", "multi-file",
            "across multiple", "entire codebase", "from scratch", "set up",
            "configure", "deploy", "pipeline", "database schema", "api endpoint",
        ]
        if any(kw in task_lower for kw in complex_keywords):
            return "complex"

        # ── Simple-query keywords ─────────────────────────────────────
        simple_keywords = [
            "what is", "how do i", "explain", "show me", "find", "search",
            "list", "tell me about", "describe", "definition of", "example of",
            "difference between", "why does", "where is",
        ]
        if any(kw in task_lower for kw in simple_keywords):
            return "simple"

        return "normal"
