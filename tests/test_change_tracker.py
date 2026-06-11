"""
Tests for change_tracker — FileChange model, ChangeTracker CRUD, undo/restore.
"""

import os
import tempfile
from pathlib import Path
import pytest
from ata_coder.change_tracker import (
    ChangeTracker,
    ChangeType,
    FileChange,
    SessionChangeManager,
)


# ═══════════════════════════════════════════════════════════════════════════════
# FileChange model
# ═══════════════════════════════════════════════════════════════════════════════

class TestFileChange:
    """FileChange dataclass and properties."""

    def test_create_text_change(self):
        """FileChange should hold write metadata correctly."""
        change = FileChange(
            id=1,
            file_path="test.py",
            change_type=ChangeType.WRITE,
            old_content=None,
            new_content="print('hello')",
        )
        assert change.id == 1
        assert change.file_path == "test.py"
        assert change.change_type == ChangeType.WRITE
        assert change.reverted is False

    def test_summary_create(self):
        """Summary for new file should show CREATE."""
        change = FileChange(
            id=1, file_path="new.py",
            change_type=ChangeType.WRITE,
            old_content=None, new_content="code",
        )
        assert "CREATE" in change.summary
        assert "#1" in change.summary

    def test_summary_edit(self):
        """Summary for edit should show EDIT with line counts."""
        change = FileChange(
            id=2, file_path="edit.py",
            change_type=ChangeType.EDIT,
            old_content="old\ncontent\n", new_content="new\ncontent\nhere\n",
        )
        assert "EDIT" in change.summary
        assert "2→3" in change.summary  # lines

    def test_summary_delete(self):
        """Summary for delete should show DELETE."""
        change = FileChange(
            id=3, file_path="delete.py",
            change_type=ChangeType.DELETE,
            old_content="bye", new_content=None,
        )
        assert "DELETE" in change.summary

    def test_summary_reverted(self):
        """Summary for reverted change should show [REVERTED]."""
        change = FileChange(
            id=1, file_path="test.py",
            change_type=ChangeType.WRITE,
            old_content=None, new_content="x",
            reverted=True,
        )
        assert "[REVERTED]" in change.summary

    def test_diff_new_file(self):
        """Diff for new file should show all lines as additions."""
        change = FileChange(
            id=1, file_path="new.py",
            change_type=ChangeType.WRITE,
            old_content=None, new_content="line1\nline2\n",
        )
        diff = change.diff
        assert "+line1" in diff
        assert "+line2" in diff

    def test_diff_deleted_file(self):
        """Diff for deleted file should show all lines as deletions."""
        change = FileChange(
            id=1, file_path="old.py",
            change_type=ChangeType.DELETE,
            old_content="gone\n", new_content=None,
        )
        diff = change.diff
        assert "-gone" in diff

    def test_diff_edit(self):
        """Diff for edit should show unified diff."""
        change = FileChange(
            id=1, file_path="edit.py",
            change_type=ChangeType.EDIT,
            old_content="hello\nworld\n", new_content="hello\nPython\n",
        )
        diff = change.diff
        assert "-world" in diff
        assert "+Python" in diff


class TestChangeTracker:
    """ChangeTracker operations."""

    def test_init_creates_backup_dir(self, tmp_path):
        """ChangeTracker should create backup directory."""
        tracker = ChangeTracker("test-session", backup_dir=tmp_path / "backups")
        assert (tmp_path / "backups").exists()

    def test_capture_write_new_file(self, tmp_path):
        """Capturing a new file write should create WRITE change."""
        test_file = tmp_path / "new.txt"
        tracker = ChangeTracker("test", backup_dir=tmp_path / "backups")
        change = tracker.capture_write(str(test_file), "Hello, world!")
        assert change is not None
        assert change.change_type == ChangeType.WRITE
        assert change.old_content is None
        assert change.new_content == "Hello, world!"

    def test_capture_write_existing_file(self, tmp_path):
        """Capturing a write to existing file should create EDIT change."""
        test_file = tmp_path / "existing.txt"
        test_file.write_text("original")
        tracker = ChangeTracker("test", backup_dir=tmp_path / "backups")
        change = tracker.capture_write(str(test_file), "modified")
        assert change.change_type == ChangeType.EDIT
        assert change.old_content == "original"
        assert change.new_content == "modified"

    def test_capture_edit(self, tmp_path):
        """Capturing an edit should create EDIT change."""
        test_file = tmp_path / "edit.txt"
        test_file.write_text("before")
        tracker = ChangeTracker("test", backup_dir=tmp_path / "backups")
        change = tracker.capture_edit(str(test_file), "before", "after")
        assert change is not None
        assert change.change_type == ChangeType.EDIT
        assert change.old_content == "before"
        assert change.new_content == "after"

    def test_capture_edit_no_change(self, tmp_path):
        """Capturing an edit with same content should return None."""
        tracker = ChangeTracker("test", backup_dir=tmp_path / "backups")
        change = tracker.capture_edit("file.txt", "same", "same")
        assert change is None

    def test_undo_single_change(self, tmp_path):
        """Undoing should revert content and mark change as reverted."""
        test_file = tmp_path / "undo.txt"
        test_file.write_text("original content")

        tracker = ChangeTracker("test", backup_dir=tmp_path / "backups")
        tracker.capture_write(str(test_file), "modified content")
        # Actually write the modification
        test_file.write_text("modified content")

        reverted = tracker.undo(1)
        assert len(reverted) == 1
        assert reverted[0].reverted is True
        # File should be back to original
        assert test_file.read_text() == "original content"

    def test_undo_all(self, tmp_path):
        """undo_all should revert all non-reverted changes."""
        tracker = ChangeTracker("test", backup_dir=tmp_path / "backups")

        file1 = tmp_path / "f1.txt"
        file2 = tmp_path / "f2.txt"
        file1.write_text("one")
        file2.write_text("two")

        tracker.capture_write(str(file1), "modified one")
        tracker.capture_write(str(file2), "modified two")
        file1.write_text("modified one")
        file2.write_text("modified two")

        reverted = tracker.undo_all()
        assert len(reverted) == 2
        assert file1.read_text() == "one"
        assert file2.read_text() == "two"

    def test_restore_change(self, tmp_path):
        """Restore should re-apply a reverted change."""
        test_file = tmp_path / "restore.txt"
        test_file.write_text("original")

        tracker = ChangeTracker("test", backup_dir=tmp_path / "backups")
        change = tracker.capture_write(str(test_file), "modified")
        test_file.write_text("modified")
        tracker.undo(1)  # revert
        assert test_file.read_text() == "original"

        restored = tracker.restore(change.id)
        assert restored is not None
        assert restored.reverted is False
        assert test_file.read_text() == "modified"

    def test_restore_nonexistent_id(self, tmp_path):
        """Restoring a non-existent change id should return None."""
        tracker = ChangeTracker("test", backup_dir=tmp_path / "backups")
        result = tracker.restore(999)
        assert result is None

    def test_list_changes(self, tmp_path):
        """list_changes should return only active changes."""
        tracker = ChangeTracker("test", backup_dir=tmp_path / "backups")
        file1 = tmp_path / "a.txt"
        file1.write_text("a")
        tracker.capture_write(str(file1), "A")
        file1.write_text("A")
        tracker.undo(1)

        active = tracker.list_changes()
        reverted = tracker.list_changes(include_reverted=True)
        assert len(active) == 0
        assert len(reverted) == 1

    def test_summary_output(self, tmp_path):
        """summary() should return a formatted string."""
        tracker = ChangeTracker("test", backup_dir=tmp_path / "backups")
        file1 = tmp_path / "s.txt"
        file1.write_text("old")
        tracker.capture_write(str(file1), "new")
        file1.write_text("new")

        summary = tracker.summary()
        assert "Session:" in summary
        assert "Changes:" in summary
        assert "s.txt" in summary

    def test_dry_run_mode(self, tmp_path):
        """In dry-run mode, undo should return empty list."""
        tracker = ChangeTracker("test", backup_dir=tmp_path / "backups")
        tracker.dry_run = True
        file1 = tmp_path / "dry.txt"
        tracker.capture_write(str(file1), "content")

        reverted = tracker.undo(1)
        assert reverted == []

    def test_dry_run_property(self, tmp_path):
        """dry_run property should be gettable/settable."""
        tracker = ChangeTracker("test", backup_dir=tmp_path / "backups")
        assert tracker.dry_run is False
        tracker.dry_run = True
        assert tracker.dry_run is True

    def test_count_active(self, tmp_path):
        """count_active should return number of non-reverted changes."""
        tracker = ChangeTracker("test", backup_dir=tmp_path / "backups")
        file1 = tmp_path / "c1.txt"
        file2 = tmp_path / "c2.txt"
        file1.write_text("1")
        file2.write_text("2")
        tracker.capture_write(str(file1), "one")
        tracker.capture_write(str(file2), "two")
        file1.write_text("one")
        file2.write_text("two")

        assert tracker.count_active() == 2
        tracker.undo(1)
        assert tracker.count_active() == 1

    def test_count_all(self, tmp_path):
        """count_all should return total changes including reverted."""
        tracker = ChangeTracker("test", backup_dir=tmp_path / "backups")
        file1 = tmp_path / "ca.txt"
        file1.write_text("old")
        tracker.capture_write(str(file1), "new")
        file1.write_text("new")
        assert tracker.count_all() == 1

    def test_diff_summary_no_changes(self, tmp_path):
        """diff_summary with no changes should show placeholder."""
        tracker = ChangeTracker("test", backup_dir=tmp_path / "backups")
        assert tracker.diff_summary() == "(no changes to show)"


class TestSessionChangeManager:
    """SessionChangeManager multi-session support."""

    def test_get_tracker(self, tmp_path):
        """get() should return a ChangeTracker for the session."""
        manager = SessionChangeManager(base_dir=tmp_path / "sessions")
        tracker = manager.get("session-1")
        assert isinstance(tracker, ChangeTracker)
        assert tracker.session_id == "session-1"

    def test_get_same_session(self, tmp_path):
        """get() with the same session_id should return the same tracker."""
        manager = SessionChangeManager(base_dir=tmp_path / "sessions")
        t1 = manager.get("same-session")
        t2 = manager.get("same-session")
        assert t1 is t2

    def test_list_sessions(self, tmp_path):
        """list_sessions should return session IDs with backup dirs."""
        manager = SessionChangeManager(base_dir=tmp_path / "sessions")
        manager.get("sess-a")
        manager.get("sess-b")
        sessions = manager.list_sessions()
        assert "sess-a" in sessions
        assert "sess-b" in sessions

    def test_cleanup_session(self, tmp_path):
        """cleanup_session should remove tracker and backup dir."""
        base = tmp_path / "sessions"
        manager = SessionChangeManager(base_dir=base)
        manager.get("clean-me")
        assert (base / "clean-me").exists()

        result = manager.cleanup_session("clean-me")
        assert result is True
        assert not (base / "clean-me").exists()

    def test_cleanup_nonexistent(self, tmp_path):
        """cleanup_session on a non-existent session should return False."""
        manager = SessionChangeManager(base_dir=tmp_path / "sessions")
        result = manager.cleanup_session("ghost")
        assert result is False
