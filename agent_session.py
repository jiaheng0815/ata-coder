"""
Agent session persistence mixin — save, auto-save, reset, shutdown.

Extracted from ``agent.py`` to reduce the core agent module size.

Contract (host class: ``CoderAgent``):
    Requires:
    - ``self.sessions`` — SessionStore | None
    - ``self._current_session_id`` — str
    - ``self._state`` — AgentState
    - ``self.tools`` — ToolExecutor
    - ``self.skills`` — SkillManager | None
    - ``self.config`` — AppConfig
    - ``self.llm`` — LLM client
    - ``self._context_manager`` — ContextManager
    Provides:
    - ``save_session()`` — manual save via /save command
    - ``_auto_save_session()`` — fire-and-forget after each task
    - ``_do_save()`` — internal serialization + index update
    - ``session_id`` — current session ID property
    - ``get_conversation_summary()`` — human-readable session stats
    - ``reset()`` — clear state for new conversation
"""

import logging
import time

from .core import AgentState

logger = logging.getLogger(__name__)


class AgentSessionMixin:
    """Session persistence: save, auto-save, reset, and summary."""

    # ── Session persistence ─────────────────────────────────────────────

    def save_session(self, session_id: str = "") -> str:
        """Save current conversation to session storage (manual /save)."""
        if not self.sessions:
            return "Session storage not available."
        sid = session_id or self._current_session_id
        if not sid:
            from .session import generate_session_id
            sid = generate_session_id("manual-save", workspace=str(self.tools.workspace))
        return self._do_save(sid)

    def _auto_save_session(self) -> None:
        """Auto-save after every task completion (fire-and-forget, best-effort)."""
        if not self.sessions:
            return
        # Generate session ID on first auto-save
        if not self._current_session_id:
            from .session import generate_session_id
            # Find first user message for the task hash
            task_hint = ""
            for msg in self._state.messages:
                if msg.get("role") == "user":
                    task_hint = msg.get("content", "")[:100]
                    break
            self._current_session_id = generate_session_id(
                task_hint or "conversation",
                skill=self.skills.active_skill.name if self.skills and self.skills.active_skill else "",
                workspace=str(self.tools.workspace),
            )
        try:
            self._do_save(self._current_session_id)
        except Exception:
            logger.warning("Auto-save failed for session %s", self._current_session_id, exc_info=True)

    def _do_save(self, sid: str) -> str:
        """Internal: persist messages + update index."""
        from .utils import sanitize_surrogates
        first_user_msg = ""
        for msg in self._state.messages:
            if msg.get("role") == "user":
                first_user_msg = sanitize_surrogates(msg.get("content", "")[:200])
                break
        self.sessions.save(
            session_id=sid,
            messages=self._state.messages,
            summary=first_user_msg,
            skill=self.skills.active_skill.name if self.skills and self.skills.active_skill else "",
            model=self.config.llm.model,
            workspace=str(self.tools.workspace),
            tool_call_count=self._state.tool_call_count,
        )
        self._current_session_id = sid
        return sid

    @property
    def session_id(self) -> str:
        return self._current_session_id

    # ── Summary & reset ─────────────────────────────────────────────────

    def get_conversation_summary(self) -> str:
        msgs = self._state.messages
        total = len(msgs)
        tool_calls = sum(1 for m in msgs if m.get("tool_calls"))
        user_msgs = sum(1 for m in msgs if m.get("role") == "user")
        tokens = self.get_token_estimate()
        return (
            f"Session: {self._current_session_id or 'unsaved'}\n"
            f"Messages: {total} ({user_msgs} user turns, {tool_calls} tool calls)\n"
            f"Tokens: ~{tokens:,} / {self.config.agent.max_context_tokens:,}\n"
            f"Skill: {self.skills.active_skill.name if self.skills and self.skills.active_skill else 'default'}\n"
            f"Model: {self.config.llm.model}"
        )

    def reset(self) -> None:
        self._state = AgentState(start_time=time.time())
        self._current_session_id = ""
        if self.skills:
            self.skills.deactivate()
        logger.info("Agent state reset")
