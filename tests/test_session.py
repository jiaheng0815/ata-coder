"""
Unit tests for session — SessionMeta serialization, generate_session_id,
and session import/export edge cases.
"""
import json
import tempfile
from pathlib import Path

import pytest
from ata_coder.session import SessionMeta, generate_session_id


# ── SessionMeta: serialization round-trip ──────────────────────────────────


class TestSessionMeta:
    """SessionMeta.from_dict/to_dict are pure data transforms."""

    def test_round_trip_full(self):
        original = SessionMeta(
            id="abc123",
            created="2026-06-22T10:00:00Z",
            updated="2026-06-22T11:00:00Z",
            message_count=42,
            tool_call_count=7,
            summary="Fix authentication bug",
            skill="debugger",
            model="gpt-4o",
            workspace="/home/user/project",
            tags=["bugfix", "auth"],
        )
        d = original.to_dict()
        restored = SessionMeta.from_dict(d)
        assert restored.id == original.id
        assert restored.message_count == 42
        assert restored.tool_call_count == 7
        assert restored.summary == "Fix authentication bug"
        assert restored.tags == ["bugfix", "auth"]

    def test_from_dict_minimal(self):
        meta = SessionMeta.from_dict({"id": "minimal"})
        assert meta.id == "minimal"
        assert meta.message_count == 0
        assert meta.tags == []

    def test_from_dict_empty(self):
        meta = SessionMeta.from_dict({})
        assert meta.id == ""
        assert meta.message_count == 0

    def test_to_dict_keys(self):
        meta = SessionMeta(id="test")
        d = meta.to_dict()
        expected_keys = {"id", "created", "updated", "message_count",
                         "tool_call_count", "summary", "skill", "model",
                         "workspace", "tags"}
        assert set(d.keys()) == expected_keys


# ── generate_session_id: hash-based session identification ─────────────────


class TestGenerateSessionId:
    """generate_session_id produces deterministic 8-8-8 hex IDs."""

    def test_format(self):
        sid = generate_session_id("Add type hints to main.py")
        parts = sid.split("-")
        assert len(parts) == 3
        assert all(len(p) == 8 for p in parts)
        assert all(all(c in "0123456789abcdef" for c in p) for p in parts)

    def test_same_inputs_same_task_part(self):
        """The task-derived portion (part 3) is deterministic across calls."""
        sid1 = generate_session_id("fix bug", "debugger", "/tmp/project")
        sid2 = generate_session_id("fix bug", "debugger", "/tmp/project")
        # Part 3 (task hash) is deterministic
        assert sid1.split("-")[2] == sid2.split("-")[2]
        # Part 1 (workspace hash) is deterministic
        assert sid1.split("-")[0] == sid2.split("-")[0]

    def test_different_task_different_id(self):
        a = generate_session_id("add feature X")
        b = generate_session_id("fix bug Y")
        assert a != b

    def test_different_workspace_different_id(self):
        a = generate_session_id("same task", workspace="/tmp/a")
        b = generate_session_id("same task", workspace="/tmp/b")
        assert a != b

    def test_empty_inputs_ok(self):
        """Empty task/skill/workspace should produce valid ID, not crash."""
        sid = generate_session_id("", "", "")
        parts = sid.split("-")
        assert len(parts) == 3
        assert all(len(p) == 8 for p in parts)

    def test_unicode_task(self):
        """Non-ASCII task descriptions must not crash."""
        sid = generate_session_id("修复登录页面的Bug 😱")
        parts = sid.split("-")
        assert len(parts) == 3


# ── Session file export edge cases ─────────────────────────────────────────


class TestSessionExport:
    """Export and import sanity checks using temp files."""

    def test_export_empty_messages(self):
        """Exporting an empty conversation should produce minimal valid JSON."""
        from ata_coder.session import SessionManager
        with tempfile.TemporaryDirectory() as tmp:
            sm = SessionManager(project_dir=tmp)
            sid = "empty-session"
            meta = sm.save(sid, [], summary="empty test")
            assert meta.id == sid
            assert meta.message_count == 0

    def test_load_nonexistent_session(self):
        """Loading a session that doesn't exist returns None."""
        from ata_coder.session import SessionManager
        with tempfile.TemporaryDirectory() as tmp:
            sm = SessionManager(project_dir=tmp)
            messages = sm.load("nonexistent")
            assert messages is None

    def test_delete_removes_from_index(self):
        from ata_coder.session import SessionManager
        with tempfile.TemporaryDirectory() as tmp:
            sm = SessionManager(project_dir=tmp)
            sm.save("to-delete", [{"role": "user", "content": "hi"}])
            assert "to-delete" in sm._index
            sm.delete("to-delete")
            assert "to-delete" not in sm._index

    def test_list_filters_by_workspace(self):
        from ata_coder.session import SessionManager
        with tempfile.TemporaryDirectory() as tmp:
            sm = SessionManager(project_dir=tmp)
            sm.save("s1", [{"role": "user", "content": "a"}], workspace="/ws1")
            sm.save("s2", [{"role": "user", "content": "b"}], workspace="/ws2")
            all_sessions = sm.list_sessions()
            ws1_only = sm.list_sessions(workspace="/ws1")
            assert len(all_sessions) >= 2
            assert len(ws1_only) == 1
            assert ws1_only[0].id == "s1"

    def test_export_markdown(self):
        from ata_coder.session import SessionManager
        with tempfile.TemporaryDirectory() as tmp:
            sm = SessionManager(project_dir=tmp)
            msgs = [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Hello **world**"},
                {"role": "assistant", "content": "Hi there!"},
            ]
            sm.save("md-test", msgs)
            md_path = Path(tmp) / "export.md"
            sm.export_markdown("md-test", str(md_path))
            content = md_path.read_text(encoding="utf-8")
            assert "Hello" in content
            assert "Hi there" in content
