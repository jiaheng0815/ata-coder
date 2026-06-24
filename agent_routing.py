"""Model routing and task complexity classification — mixin for CoderAgent."""
import logging
import re

from .settings import get_settings

logger = logging.getLogger(__name__)


class ModelRoutingMixin:
    """Model routing based on task complexity heuristics and effort level.

    Contract (host class: ``CoderAgent``):
        Requires:
        - ``self.config`` — AppConfig instance
        Provides:
        - ``_classify_task()`` — scored heuristic (no API call)
        - ``_ai_classify()`` — scored heuristic classification for ambiguous cases
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
        Score-based task complexity classification (NO extra LLM call).

        Returns 'simple', 'complex', or 'normal'.

        Design:
        - Length shortcuts for extreme cases (configurable thresholds)
        - Middle-ground tasks (60–500 chars) use a weighted score:
          positive signals → complex, negative signals → simple
        - Score thresholds: >= 3 → complex, <= -2 → simple, else normal

        This replaces the old binary keyword-match approach which could not
        handle mixed-signal tasks like "implement a hello world" (complex
        keyword in a simple task) or "what is causing the deadlock in my
        8-file async pipeline" (simple keyword in a complex task).
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

        # ── Scored heuristic for middle-ground tasks ────────────────────
        score = 0

        # ---- Complexity signals (+1 to +3) ----

        # Multi-step / numbered instructions (e.g. "1. do X\n2. then Y")
        if re.search(r'\d+[\.\)]\s', task):
            score += 3
        if re.search(r'\b(steps?|first\b.*\bthen\b|next\b.*\bfinally\b|after that)\b',
                     task_lower):
            score += 2

        # Code / file references present — implies reading/writing code
        if '`' in task or '```' in task:
            score += 2
        if re.search(r'\b[a-zA-Z0-9_/]+\.(py|js|ts|rs|go|java|cpp|c|h|rb|sh|toml|yaml|json|sql)\b',
                     task_lower):
            score += 1

        # Error / bug / crash language — debugging is harder than Q&A
        if re.search(r'\b(error|bug|crash|fail(?:ed|ure|s)?|broken|doesn\'?t work|'
                     r'not working|incorrect|wrong|unexpected|traceback|stack trace)\b',
                     task_lower):
            score += 2

        # Creation / modification verbs — high agency required
        if re.search(r'\b(implement|build|create|write|refactor|migrate|'
                     r'redesign|architect|restructure|reorganize|overhaul)\b',
                     task_lower):
            score += 2

        # Cross-cutting concern keywords
        if re.search(r'\b(security|performance|concurrency|race condition|'
                     r'deadlock|memory leak|scalability|latency)\b',
                     task_lower):
            score += 2

        # ---- Simplicity signals (-1 to -3) ----

        # Pure question / explanation patterns
        if re.search(r'^(what|how|why|when|where|who|can you|could you|'
                     r'is it|does|are there|do you)\b', task_lower):
            score -= 2

        # Single file / small scope / trivial
        if re.search(r'\b(single|small|simple|quick|basic|just|only|trivial|'
                     r'minor|tiny)\b', task_lower):
            score -= 1

        # Definition / lookup requests
        if re.search(r'\b(define|definition|meaning|what does|explain|describe)\b',
                     task_lower):
            score -= 1

        # ── Decision ──────────────────────────────────────────────────
        if score >= 3:
            return "complex"
        if score <= -2:
            return "simple"
        return "normal"
