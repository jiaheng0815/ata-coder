"""Context compaction and token budget management — mixin for CoderAgent.

Delegates all context operations to ContextManager.  This mixin is now a
thin wrapper that provides the same public API while eliminating duplicated
logic, avoiding deep copies, and reusing the summarisation LLM client.
"""

import copy
import logging

from .types import Message
from .clawd_integration import get_clawd
from .model_router import get_subagent_model
import contextlib

try:
    from .prompt_compressor import get_compressor, is_available as _llmlingua_available
except ImportError:
    _llmlingua_available = lambda: False
    get_compressor = lambda: None

logger = logging.getLogger(__name__)


class CompactionMixin:
    """Context window compaction — thin wrapper around ContextManager.

    Contract (host class: ``CoderAgent``):
        Requires:
        - ``self._ctx`` — ContextManager instance (O(1) token tracking)
        - ``self.config`` — AppConfig instance
        - ``self._chat()`` — async method → Message (non-streaming LLM call)
        Provides:
        - ``compact()`` — LLM summarization of archive segment
        - ``_force_truncate()`` — last-resort truncation when LLM fails
    """

    # ── Compaction token budget (class-level defaults, overridable) ───────
    RECENT_TOKEN_BUDGET = 80_000   # max tokens to keep in the recent segment
    COMPACT_IF_FEWER_THAN = 6      # skip compaction if fewer than this many msgs

    # ── Core compaction ───────────────────────────────────────────────────

    async def compact(self) -> str:
        """Compact conversation by summarising old messages.

        Strategy: keep system prompt + recent messages up to
        RECENT_TOKEN_BUDGET tokens, summarise everything in between using
        a cheap LLM call.  Falls back to a lightweight extractive summary
        if the API call fails.

        Delegates segment-splitting to ContextManager to avoid the
        duplicated walk-backwards logic that was previously shared with
        _force_truncate.
        """
        cm = self._context_manager
        if not cm.can_compact():
            return "Already compact."

        # Clawd: PreCompact
        get_clawd().compact()

        system_msg, recent, archive = cm.split_into_segments()

        if not archive:
            return "Already compact (all messages fit in recent budget)."

        # Extract summary metadata from the archive segment
        tool_count = sum(1 for m in archive if m.get("tool_calls"))
        user_msgs = [m.get("content", "")[:200] for m in archive if m.get("role") == "user"]
        file_ops = cm.collect_file_ops(archive)

        summary = await self._summarise_messages(archive, file_ops, user_msgs, tool_count)

        old_count = len(cm.messages)
        old_tokens = cm.token_total

        truncated: list[Message] = []
        if system_msg:
            truncated.append(system_msg)
        truncated.append({
            "role": "user",
            "content": "[Conversation summary]\n" + summary,
        })
        truncated.append({
            "role": "assistant",
            "content": "Understood. I'll continue with the remaining context using the summary above.",
        })
        truncated.extend(recent)

        cm.replace_all(truncated)
        self._cached_system_prompt = None  # system msg may have shifted
        self._state.messages = list(cm.messages)  # sync for backward compat (copy — avoid shared ref)

        new_tokens = cm.token_total
        logger.info("Compacted: %d→%d msgs, ~%d→%d tokens (files: %d, tools: %d)",
                    old_count, len(truncated), old_tokens, new_tokens,
                    len(file_ops), tool_count)
        return (f"Compacted from {old_count}→{len(truncated)} messages "
                f"(~{old_tokens:,}→~{new_tokens:,} tokens, {len(file_ops)} files, {tool_count} tool calls).")

    def _force_truncate(self) -> None:
        """Drop the oldest non-system messages when we exceed 95% of max tokens.

        Called only as a last resort after compaction has already run.
        Delegates to ContextManager.build_truncated_list() — no more
        duplicated walk-backwards.
        """
        cm = self._context_manager
        if len(cm.messages) <= 6:
            return
        truncated, result = cm.build_truncated_list()
        cm.replace_all(truncated)
        self._cached_system_prompt = None
        self._state.messages = list(cm.messages)  # sync (copy — avoid shared ref)
        logger.warning("Force-truncated: %d → %d messages (~%d tokens kept)",
                       result.old_count, result.new_count, result.new_tokens)

    # ── LLMLingua compression (local, zero-API-cost) ──────────────────────

    def _compress_via_llmlingua(self, archive: list[Message], file_ops: list[str],
                                 user_msgs: list[str], tool_count: int) -> str | None:
        """Try LLMLingua local compression before falling back to LLM summarisation.

        Returns the compressed summary string, or None if LLMLingua is
        unavailable or fails.  LLMLingua runs entirely locally — no API cost.
        """
        if not _llmlingua_available():
            return None
        try:
            compressor = get_compressor()
            if compressor is None or not compressor.available:
                return None

            # Build structured text for the compressor to preserve
            parts: list[str] = []
            if file_ops:
                parts.append(f"Files modified: {', '.join(file_ops[:20])}")
            if tool_count:
                parts.append(f"Tool calls: {tool_count}")
            if user_msgs:
                parts.append(f"User requests: {'; '.join(user_msgs[:5])}")
            parts.append("---")

            # Feed the archive messages through the compressor
            compressed = compressor.compress_messages(archive, target_ratio=0.4)
            if not compressed or len(compressed) < 20:
                return None

            # Assemble a compact but structured summary
            header = (
                f"Compressed {len(archive)} messages ({tool_count} tool calls). "
            )
            if file_ops:
                header += f"Files: {', '.join(file_ops[:10])}. "
            return header + compressed
        except Exception:  # noqa: BLE001
            logger.debug("LLMLingua compression failed, will use LLM fallback")
            return None

    # ── Summarisation (reuses a single cheap LLM client) ──────────────────

    async def _summarise_messages(self, archive: list[Message], file_ops: list[str],
                                  user_msgs: list[str], tool_count: int) -> str:
        """Generate a summary of the archive conversation segment.

        Attempts LLMLingua local compression first (zero API cost), then a
        cheap LLM call, then falls back to a lightweight extractive summary
        so the user never loses context entirely.  The summarisation
        client is created once and reused across compactions.
        """
        # ── LLMLingua local compression (fast, zero API cost) ───────────
        llmlingua_result = self._compress_via_llmlingua(
            archive, file_ops, user_msgs, tool_count
        )
        if llmlingua_result:
            return llmlingua_result

        # ── LLM-based summary (best effort) ──────────────────────────────
        try:
            summary_prompt = (
                "You are summarising a conversation segment that will be ARCHIVED. "
                "The agent will ONLY see this summary going forward — the original "
                "messages will be permanently removed to save context tokens.\n\n"
                "Your summary MUST preserve everything the agent needs to continue "
                "working seamlessly. A bad summary causes the agent to repeat work, "
                "forget decisions, or lose critical context.\n\n"
                "Structure your summary as follows:\n\n"
                "## User's Goal\n"
                "What the user is trying to accomplish — the overarching task, not "
                "just individual requests. Include any explicit preferences or "
                "constraints they mentioned.\n\n"
                "## Decisions Made\n"
                "Key technical decisions and their rationale. Architecture choices, "
                "library selections, naming conventions, API designs. WHY each "
                "decision was made — not just what was decided.\n\n"
                "## Files Changed (with purpose)\n"
                "List each file and what was done to it and WHY:\n"
                "- `path/file.py` — [what changed] because [reason]\n\n"
                "## Errors Encountered & Resolutions\n"
                "Any errors that occurred and how they were fixed. Include error "
                "messages if they reveal important constraints. This prevents the "
                "agent from repeating the same mistakes.\n\n"
                "## Work in Progress\n"
                "What was started but not finished. What the next step should be. "
                "Any open questions that need resolution.\n\n"
                "## Context for Continuity\n"
                "Any other information the agent will need to pick up where it "
                "left off: environment details, version numbers, API responses, "
                "test output, the user's coding style preferences.\n\n"
                "Be thorough but waste no words. Every sentence should carry "
                "information the agent cannot recover from the remaining recent "
                "messages. If a fact is already in the recent messages, don't "
                "repeat it here.\n\n"
                "---\n"
                f"Files modified: {', '.join(file_ops) if file_ops else 'none'}\n"
                f"Tool calls in segment: {tool_count}\n"
                f"User requests: {'; '.join(user_msgs[:8])}\n"
            )
            sc = getattr(self, '_summary_llm', None)
            if sc is None or getattr(sc, '_client', None) is None:
                from .llm_client import LLMClient
                summary_config = copy.deepcopy(self.llm.config)
                summary_config.model = get_subagent_model()
                sc = LLMClient(summary_config)
                # Close previous client (if any) to avoid connection leak
                old_sc = getattr(self, '_summary_llm', None)
                if old_sc is not None and old_sc is not sc:
                    with contextlib.suppress(Exception):
                        await old_sc.close()
                self._summary_llm = sc  # cache for reuse
            try:
                resp = await sc.chat([{"role": "user", "content": summary_prompt}], tools=[])
            except Exception:  # noqa: BLE001
                # Client may have been closed — recreate and retry once
                from .llm_client import LLMClient
                summary_config = copy.deepcopy(self.llm.config)
                summary_config.model = get_subagent_model()
                old_sc = sc
                # Clear cached reference before construction so a failed
                # LLMClient() doesn't leave a closed client on self.
                self._summary_llm = None
                sc = LLMClient(summary_config)
                self._summary_llm = sc
                # Close the failed client to avoid connection leak
                with contextlib.suppress(Exception):
                    await old_sc.close()
                resp = await sc.chat([{"role": "user", "content": summary_prompt}], tools=[])
            llm_summary = (resp.get("content") or "").strip()
            if llm_summary:
                parts = [llm_summary]
                if file_ops:
                    parts.append(f"\nFiles touched: {', '.join(file_ops[:10])}")
                return "\n".join(parts)
        except Exception:  # noqa: BLE001
            logger.debug("LLM summarisation unavailable, using extractive fallback")

        # ── Extractive fallback ─────────────────────────────────────────
        parts = [f"Summarised {len(archive)} messages ({tool_count} tool calls)."]
        if user_msgs:
            parts.append(f"Topics: {'; '.join(user_msgs[:5])}")
        if file_ops:
            parts.append(f"Files modified: {', '.join(file_ops[:10])}")
        return "\n".join(parts)
