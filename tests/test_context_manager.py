"""
Unit tests for context_manager — O(1) token tracking, segment splitting,
compaction decisions, truncation, and file-op extraction.
"""
import pytest
from ata_coder.context_manager import ContextManager, CompactionResult


# ── Helpers ────────────────────────────────────────────────────────────────


def _msg(role="user", content="hello", tool_calls=None):
    m = {"role": role, "content": content}
    if tool_calls:
        m["tool_calls"] = tool_calls
    return m


# ── _msg_stable_key: content-hash identity ─────────────────────────────────


class TestMsgStableKey:
    """Content-hash keying for stable cache identity across rebuilds."""

    def test_same_content_same_key(self):
        a = _msg("user", "hello")
        b = _msg("user", "hello")
        assert ContextManager._msg_stable_key(a) == ContextManager._msg_stable_key(b)

    def test_different_role_different_key(self):
        a = _msg("user", "hello")
        b = _msg("assistant", "hello")
        assert ContextManager._msg_stable_key(a) != ContextManager._msg_stable_key(b)

    def test_different_content_different_key(self):
        a = _msg("user", "hello")
        b = _msg("user", "world")
        assert ContextManager._msg_stable_key(a) != ContextManager._msg_stable_key(b)

    def test_tool_calls_affect_key(self):
        a = _msg("assistant", "ok", [{"id": "1", "function": {"name": "grep"}}])
        b = _msg("assistant", "ok", [{"id": "2", "function": {"name": "glob"}}])
        assert ContextManager._msg_stable_key(a) != ContextManager._msg_stable_key(b)

    def test_empty_content_ok(self):
        key = ContextManager._msg_stable_key({"role": "user"})
        assert isinstance(key, int)

    def test_none_content_ok(self):
        key = ContextManager._msg_stable_key({"role": "user", "content": None})
        assert isinstance(key, int)


# ── O(1) token tracking ────────────────────────────────────────────────────


class TestTokenTracking:
    """append, replace_all, get_msg_tokens, and token_total."""

    def test_append_updates_total(self):
        cm = ContextManager()
        before = cm.token_total
        cm.append(_msg("user", "hello world"))
        assert cm.token_total > before
        assert len(cm.messages) == 1

    def test_append_multiple_accumulates(self):
        cm = ContextManager()
        cm.append(_msg("user", "first"))
        t1 = cm.token_total
        cm.append(_msg("user", "second"))
        assert cm.token_total > t1
        assert len(cm.messages) == 2

    def test_replace_all_resets(self):
        cm = ContextManager()
        cm.append(_msg("user", "old"))
        old_total = cm.token_total
        cm.replace_all([_msg("user", "fresh start")])
        assert len(cm.messages) == 1
        assert cm.messages[0]["content"] == "fresh start"

    def test_replace_all_reuses_cache(self):
        """Messages with same content hash should reuse cached token counts."""
        cm = ContextManager()
        m1 = _msg("user", "persistent content")
        cm.append(m1)
        t_after_append = cm.token_total

        # Replace with same-content message (different Python object)
        m2 = _msg("user", "persistent content")
        cm.replace_all([m2])
        # Token total should match — cache reuse
        assert cm.token_total == t_after_append

    def test_get_msg_tokens_returns_int(self):
        cm = ContextManager()
        tokens = cm.get_msg_tokens(_msg("user", "hello"))
        assert isinstance(tokens, int)
        assert tokens > 0

    def test_get_msg_tokens_cached_on_reuse(self):
        cm = ContextManager()
        msg = _msg("user", "cache me")
        first = cm.get_msg_tokens(msg)
        second = cm.get_msg_tokens(msg)
        assert first == second


# ── Segment splitting ──────────────────────────────────────────────────────


class TestSplitIntoSegments:
    """split_into_segments partitions messages by token budget."""

    def test_empty_messages(self):
        cm = ContextManager()
        sys_msg, recent, archive = cm.split_into_segments()
        assert sys_msg is None
        assert recent == []
        assert archive == []

    def test_only_user_messages(self):
        cm = ContextManager()
        cm.append(_msg("user", "hello"))
        cm.append(_msg("user", "world"))
        sys_msg, recent, archive = cm.split_into_segments()
        assert sys_msg is None
        assert len(recent) > 0
        # All non-system messages should be in recent (below default 80k budget)
        assert len(recent) == 2
        assert archive == []

    def test_system_message_extracted(self):
        cm = ContextManager()
        cm.append(_msg("system", "You are helpful."))
        cm.append(_msg("user", "hello"))
        sys_msg, recent, _ = cm.split_into_segments()
        assert sys_msg is not None
        assert sys_msg["role"] == "system"
        assert recent[0]["role"] == "user"

    def test_archive_when_over_budget(self):
        """When token total exceeds recent budget, old messages go to archive."""
        cm = ContextManager()
        cm.recent_token_budget = 5  # extremely tight budget
        cm.append(_msg("user", "one two three four five six seven eight"))  # many tokens
        cm.append(_msg("user", "hi"))
        sys_msg, recent, archive = cm.split_into_segments()
        # "hi" should be recent (last message), the long one in archive
        assert len(recent) == 1
        assert recent[0]["content"] == "hi"
        assert len(archive) >= 1

    def test_single_huge_message_not_lost(self):
        """One message exceeding the budget is kept (gracefully)."""
        cm = ContextManager()
        cm.recent_token_budget = 1
        cm.append(_msg("user", "a" * 200))  # single large message
        _, recent, _ = cm.split_into_segments()
        assert len(recent) == 1


# ── Compaction decisions ───────────────────────────────────────────────────


class TestCompactionDecisions:
    """should_compact, needs_force_truncate, can_compact."""

    def test_no_config_no_compact(self):
        cm = ContextManager(config=None)
        assert cm.should_compact() is False
        assert cm.needs_force_truncate() is False

    def test_below_threshold_no_compact(self):
        from ata_coder.config import AgentConfig
        config = AgentConfig()
        config.effective_context_tokens = 999_999
        cm = ContextManager(config=config)
        cm.append(_msg("user", "hi"))
        assert cm.should_compact() is False

    def test_above_threshold_triggers_compact(self):
        from ata_coder.config import AgentConfig
        config = AgentConfig()
        config.effective_context_tokens = 0  # trigger on any token
        cm = ContextManager(config=config)
        cm.append(_msg("user", "hello"))
        assert cm.token_total > 0
        assert cm.should_compact() is True

    def test_can_compact_with_enough_history(self):
        cm = ContextManager()
        for _ in range(10):
            cm.append(_msg("user", "msg"))
        assert cm.can_compact() is True

    def test_cannot_compact_with_too_few_messages(self):
        cm = ContextManager()
        cm.compact_if_fewer_than = 100
        cm.append(_msg("user", "only one"))
        assert cm.can_compact() is False


# ── Truncation ─────────────────────────────────────────────────────────────


class TestBuildTruncatedList:
    """build_truncated_list produces a valid truncated message list."""

    def test_truncated_shorter_than_original(self):
        cm = ContextManager()
        cm.recent_token_budget = 5
        for i in range(20):
            cm.append(_msg("user", f"message number {i} with padding" * 3))
        truncated, result = cm.build_truncated_list()
        assert len(truncated) < len(cm.messages)
        assert result.truncated is True
        assert isinstance(result, CompactionResult)

    def test_truncated_preserves_system(self):
        cm = ContextManager()
        cm.append(_msg("system", "You are helpful."))
        cm.append(_msg("user", "hi"))
        truncated, _ = cm.build_truncated_list()
        assert truncated[0]["role"] == "system"

    def test_truncation_marker_present(self):
        cm = ContextManager()
        cm.append(_msg("user", "long message " * 20))
        cm.recent_token_budget = 1
        truncated, _ = cm.build_truncated_list()
        markers = [m["content"] for m in truncated if "truncated" in str(m.get("content", "")).lower()]
        assert len(markers) >= 1


# ── File operations extraction ─────────────────────────────────────────────


class TestCollectFileOps:
    """collect_file_ops extracts file paths from write_file/edit_file tool calls."""

    def test_no_ops(self):
        assert ContextManager.collect_file_ops([]) == []

    def test_write_file_extracted(self):
        msgs = [_msg("assistant", "", [{
            "id": "1", "type": "function",
            "function": {"name": "write_file", "arguments": '{"file_path": "/tmp/a.py", "content": "x"}'},
        }])]
        ops = ContextManager.collect_file_ops(msgs)
        assert "/tmp/a.py" in ops

    def test_edit_file_extracted(self):
        msgs = [_msg("assistant", "", [{
            "id": "2", "type": "function",
            "function": {"name": "edit_file", "arguments": '{"file_path": "/tmp/b.py", "old_string": "x", "new_string": "y"}'},
        }])]
        ops = ContextManager.collect_file_ops(msgs)
        assert "/tmp/b.py" in ops

    def test_non_file_tool_ignored(self):
        msgs = [_msg("assistant", "", [{
            "id": "3", "type": "function",
            "function": {"name": "grep", "arguments": '{"pattern": "foo"}'},
        }])]
        assert ContextManager.collect_file_ops(msgs) == []

    def test_multiple_tool_calls(self):
        msgs = [_msg("assistant", "", [
            {"id": "4", "function": {"name": "write_file", "arguments": '{"file_path": "/a"}'}},
            {"id": "5", "function": {"name": "edit_file", "arguments": '{"file_path": "/b"}'}},
        ])]
        ops = ContextManager.collect_file_ops(msgs)
        assert set(ops) == {"/a", "/b"}

    def test_malformed_json_skipped(self):
        msgs = [_msg("assistant", "", [{
            "id": "6", "function": {"name": "write_file", "arguments": "{bad json"},
        }])]
        ops = ContextManager.collect_file_ops(msgs)
        assert ops == []  # should not raise


# ── extract_important_snippets: high-signal archive extraction ──────────────

class TestExtractSnippets:
    """extract_important_snippets preserves errors, code, and instructions."""

    def test_error_tool_result_captured(self):
        archive = [
            {"role": "tool", "content": "Error: File not found: /tmp/x.py\nTraceback: ..."},
        ]
        snippets = ContextManager.extract_important_snippets(archive)
        assert any("ERROR" in s and "File not found" in s for s in snippets)

    def test_code_block_extracted(self):
        archive = [
            {"role": "assistant",
             "content": "Here's the fix:\n```python\ndef foo():\n    return 42\n```\nThat should work."},
        ]
        snippets = ContextManager.extract_important_snippets(archive)
        assert any("[code]" in s and "def foo" in s for s in snippets)

    def test_user_instruction_preserved(self):
        archive = [
            {"role": "user",
             "content": "Please refactor the auth module to use JWT instead of session cookies"},
        ]
        snippets = ContextManager.extract_important_snippets(archive)
        assert any("[user]" in s and "refactor" in s for s in snippets)

    def test_short_user_message_skipped(self):
        """Messages under 30 chars are not worth preserving as snippets."""
        archive = [
            {"role": "user", "content": "ok"},
            {"role": "user", "content": "yes"},
        ]
        snippets = ContextManager.extract_important_snippets(archive)
        assert not any("[user]" in s for s in snippets)

    def test_max_items_respected(self):
        archive = [
            {"role": "user", "content": "Task " + str(i) + ": do something important and complex"} for i in range(30)
        ]
        snippets = ContextManager.extract_important_snippets(archive, max_items=5)
        assert len(snippets) <= 5

    def test_truncated_output_captured(self):
        archive = [
            {"role": "tool", "content": "Build output… [truncated at line 5000]"},
        ]
        snippets = ContextManager.extract_important_snippets(archive)
        assert any("TRUNCATED" in s for s in snippets)

    def test_empty_archive_returns_empty(self):
        assert ContextManager.extract_important_snippets([]) == []

    def test_error_snippet_truncated_at_300_chars(self):
        archive = [
            {"role": "tool", "content": "Error: " + ("x" * 500)},
        ]
        snippets = ContextManager.extract_important_snippets(archive)
        error_snippet = [s for s in snippets if "ERROR" in s]
        assert len(error_snippet) > 0
        # Should be truncated (300 + "…" overhead)
        assert len(error_snippet[0]) <= 320
