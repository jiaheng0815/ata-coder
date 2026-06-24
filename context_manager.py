"""
Unified context window manager for ATA Coder.

Owns:
- O(1) incremental token tracking via id()-keyed cache
- Segment-based message management (SYSTEM / RECENT / ARCHIVE)
- Adaptive compaction decisions with configurable budgets
- Force truncation as a last resort
- File operation extraction for summary metadata

Replaces the ad-hoc compaction logic previously spread across
CompactionMixin._force_truncate() and CompactionMixin.compact().
"""

import json
import logging
from dataclasses import dataclass
from typing import Optional

from .types import Message
from .token_counter import TokenCounter

logger = logging.getLogger(__name__)


@dataclass
class CompactionResult:
    """Result of a compaction operation."""
    old_count: int
    new_count: int
    old_tokens: int
    new_tokens: int
    file_ops_count: int
    tool_call_count: int
    summary: str = ""
    truncated: bool = False


class ContextManager:
    """Central context window manager with O(1) token tracking.

    Usage:
        cm = ContextManager(config.agent)
        cm.replace_all(initial_messages)
        ...
        cm.append(new_message)          # O(1) token update
        if cm.should_compact():         # O(1)
            await compact(cm)
        if cm.needs_force_truncate():   # O(1)
            cm.force_truncate()
    """

    # ── Default budgets (overridable via AgentConfig) ─────────────────────

    DEFAULT_RECENT_TOKEN_BUDGET = 80_000
    DEFAULT_COMPACT_IF_FEWER_THAN = 6

    @staticmethod
    def _msg_stable_key(msg: Message) -> int:
        """Content-based hash key — survives message-list rebuilds.

        Uses ``hash()`` over role+content+tool_calls so that when
        ``replace_all()`` passes in equivalent messages (same content,
        different Python objects), the cached token count is reused
        instead of recomputed from scratch.

        ``hash()`` is randomized per-process (PYTHONHASHSEED) but
        stable within a single process lifetime — safe for an
        in-memory cache.
        """
        role = msg.get("role", "")
        content = msg.get("content", "") or ""
        tool_calls = msg.get("tool_calls")
        raw = f"{role}|{content}"
        if tool_calls:
            raw += f"|{json.dumps(tool_calls, sort_keys=True, ensure_ascii=False)}"
        return hash(raw)

    def __init__(self, config=None):
        self._config = config
        self.messages: list[Message] = []
        self._token_total: int = 0
        self._msg_tokens: dict[int, int] = {}  # content_hash(msg) -> token count

        # Configurable budgets (read from AgentConfig if available)
        self.recent_token_budget: int = getattr(
            config, 'recent_token_budget', self.DEFAULT_RECENT_TOKEN_BUDGET
        )
        self.compact_if_fewer_than: int = getattr(
            config, 'compact_if_fewer_than', self.DEFAULT_COMPACT_IF_FEWER_THAN
        )
        self._counter = TokenCounter.for_model(
            getattr(getattr(config, 'llm', None), 'model', '')
        )

    # ── O(1) token tracking ──────────────────────────────────────────────

    @property
    def token_total(self) -> int:
        """Running total — O(1), no iteration, no encoding."""
        return self._token_total

    def append(self, msg: Message) -> None:
        """Append a message and update the running token total (O(1) with cache hit)."""
        count = self._counter.count_one(msg)
        self._msg_tokens[self._msg_stable_key(msg)] = count
        self._token_total += count
        self.messages.append(msg)

    def replace_all(self, new_messages: list[Message]) -> None:
        """Replace the entire message list and recompute totals.

        Used after compaction, truncation, or state reset.
        """
        self.messages = list(new_messages)  # copy to decouple from caller
        self._rebuild_token_total()

    def _rebuild_token_total(self) -> None:
        """Recompute token total — reuses cached counts via content hash.

        After compaction or reset the message list may contain new Python
        objects.  Because the cache key is a content hash, a message that
        was NOT changed by compaction keeps its old token count, avoiding
        a full re-encode of the entire conversation.
        """
        old_cache = self._msg_tokens
        self._msg_tokens = {}
        self._token_total = 0
        for msg in self.messages:
            key = self._msg_stable_key(msg)
            if key in old_cache:
                count = old_cache[key]
            else:
                count = self._counter.count_one(msg)
            self._msg_tokens[key] = count
            self._token_total += count

    def get_msg_tokens(self, msg: Message) -> int:
        """Look up cached token count for a message, or compute and cache it."""
        key = self._msg_stable_key(msg)
        if key in self._msg_tokens:
            return self._msg_tokens[key]
        count = self._counter.count_one(msg)
        self._msg_tokens[key] = count
        return count

    # ── Segment management (shared walk-backwards — no more duplication) ─

    def split_into_segments(self) -> tuple[Optional[Message], list[Message], list[Message]]:
        """Split messages into (system_msg, recent_msgs, archive_msgs).

        The "recent" segment keeps messages up to recent_token_budget tokens,
        walking backwards from the end. The "archive" (middle) segment is
        everything between system and recent — this is what gets summarized
        during compaction.

        Returns:
            (system_msg or None, recent_messages, archive_messages)
        """
        if not self.messages:
            return None, [], []

        first = self.messages[0]
        has_system = isinstance(first, dict) and first.get("role") == "system"
        all_but_system = self.messages[1:] if has_system else self.messages[:]

        # Walk backwards through recent messages, accumulating up to the budget
        recent: list[Message] = []
        recent_tokens = 0
        for msg in reversed(all_but_system):
            msg_tokens = self.get_msg_tokens(msg)
            if recent_tokens + msg_tokens > self.recent_token_budget:
                if not recent:
                    # Single huge message — include it anyway but stop after
                    recent.insert(0, msg)
                    recent_tokens += msg_tokens
                break
            recent.insert(0, msg)
            recent_tokens += msg_tokens

        # The archive is everything NOT in recent and NOT the system msg
        kept_count = len(recent)
        archive = all_but_system[:-kept_count] if kept_count > 0 else all_but_system[:]

        system_msg = first if has_system else None
        return system_msg, recent, archive

    # ── Compaction decisions (all O(1)) ──────────────────────────────────

    def should_compact(self) -> bool:
        """O(1): is token total above the effective context limit?"""
        if self._config is None:
            return False
        return self._token_total > self._config.effective_context_tokens

    def needs_force_truncate(self) -> bool:
        """O(1): is token total above 95% of hard limit?"""
        if self._config is None:
            return False
        return self._token_total > self._config.max_context_tokens * 0.95

    def can_compact(self) -> bool:
        """Is there enough history to make compaction worthwhile?"""
        return len(self.messages) > self.compact_if_fewer_than

    # ── Truncation builder (pure — does not mutate) ──────────────────────

    def build_truncated_list(self) -> tuple[list[Message], CompactionResult]:
        """Build a truncated message list keeping only system + recent budget.

        Returns (truncated_messages, result_metadata).
        Does NOT modify self.messages — the caller applies the change.
        """
        system_msg, recent, archive = self.split_into_segments()

        truncated: list[Message] = []
        if system_msg:
            truncated.append(system_msg)
        truncated.append({
            "role": "user",
            "content": "[Conversation truncated — token limit reached]",
        })
        truncated.append({
            "role": "assistant",
            "content": "Understood. I'll continue with the most recent context.",
        })
        truncated.extend(recent)

        old_count = len(self.messages)
        old_tokens = self._token_total
        new_count = len(truncated)
        new_tokens = sum(self.get_msg_tokens(m) for m in truncated)

        result = CompactionResult(
            old_count=old_count,
            new_count=new_count,
            old_tokens=old_tokens,
            new_tokens=new_tokens,
            file_ops_count=0,
            tool_call_count=sum(1 for m in archive if m.get("tool_calls")),
            truncated=True,
        )
        return truncated, result

    # ── File operations extraction ───────────────────────────────────────

    @staticmethod
    def collect_file_ops(messages: list[Message]) -> list[str]:
        """Collect file paths from write_file/edit_file tool calls."""
        ops: list[str] = []
        for m in messages:
            for tc in m.get("tool_calls", []):
                fn = tc.get("function", {})
                if fn.get("name") in ("write_file", "edit_file"):
                    try:
                        args = json.loads(fn.get("arguments", "{}"))
                        fp = args.get("file_path", "")
                        if fp:
                            ops.append(fp)
                    except json.JSONDecodeError:
                        pass
        return ops

    @staticmethod
    def extract_important_snippets(archive: list[Message], max_items: int = 15) -> list[str]:
        """Extract high-signal snippets from archive messages for fallback summaries.

        Scans for errors, code blocks, user instructions, and tool results —
        the information an agent most needs to continue after compaction.
        Returns up to *max_items* snippets, newest first.
        """
        snippets: list[str] = []
        for m in reversed(archive):
            if len(snippets) >= max_items:
                break
            role = m.get("role", "")
            content = str(m.get("content", ""))

            # ── Tool results: capture errors and truncated outputs ──────
            if role == "tool" and content:
                content_lower = content.lower()
                is_error = any(kw in content_lower for kw in (
                    "error", "traceback", "exception", "failed", "permission denied",
                    "not found", "timeout", "command not found",
                ))
                is_truncated = "[truncated" in content or "output capped" in content
                if is_error:
                    # Keep error snippet (first 300 chars — the root cause)
                    snippet = content[:300]
                    if len(content) > 300:
                        snippet += "…"
                    snippets.append(f"[ERROR] {snippet}")
                elif is_truncated:
                    snippets.append(f"[TRUNCATED OUTPUT] {content[:200]}…")
                elif len(snippets) < max_items:
                    # Keep non-error tool output if we have room
                    snippets.append(f"[tool output] {content[:150]}")

            # ── Assistant messages: capture code blocks ──────────────────
            elif role == "assistant" and content:
                # Extract first code block (if any) as a snippet
                if "```" in content:
                    # Find the first code block
                    start = content.find("```")
                    end = content.find("```", start + 3)
                    if end > start:
                        code = content[start:end + 3]
                        # Keep it brief
                        if len(code) > 250:
                            code = code[:250] + "\n…\n```"
                        snippets.append(f"[code] {code}")
                        continue

            # ── User messages: preserve instructions ──────────────────────
            elif role == "user" and content and len(snippets) < max_items:
                # Only capture user messages that look like instructions
                if len(content) > 30 and not content.startswith("["):
                    snippets.append(f"[user] {content[:200]}")

        snippets.reverse()  # chronological order
        return snippets
