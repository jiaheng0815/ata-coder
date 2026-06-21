"""
Tests for fool_proof — FoolProofEngine unified safety evaluation.
"""

from ata_coder.fool_proof import FoolProofEngine, ActionRequired
from ata_coder.safety_guard import SafetyGuard, RiskLevel
from ata_coder.permissions import PermissionStore, PermissionMode
from ata_coder.change_tracker import ChangeTracker


class TestFoolProofEngineInit:
    """FoolProofEngine initialization."""

    def test_init_with_defaults(self, tmp_path):
        """Engine should init with a SafetyGuard."""
        engine = FoolProofEngine(tmp_path)
        assert isinstance(engine.guard, SafetyGuard)
        assert engine.stats["blocks"] == 0

    def test_init_with_all_deps(self, tmp_path):
        """Engine should accept all dependencies."""
        guard = SafetyGuard(tmp_path)
        permissions = PermissionStore()
        tracker = ChangeTracker("test", backup_dir=tmp_path / "backups")
        engine = FoolProofEngine(tmp_path, permissions, tracker, guard)
        assert engine.permissions is permissions
        assert engine.tracker is tracker
        assert engine.guard is guard


class TestFoolProofEngineEvaluate:
    """FoolProofEngine.evaluate — core logic."""

    def test_read_file_always_proceed(self, tmp_path):
        """read_file should always be PROCEED."""
        engine = FoolProofEngine(tmp_path)
        check = engine.evaluate("read_file", {"file_path": "test.txt"})
        assert check.allowed is True
        assert check.action == ActionRequired.PROCEED

    def test_glob_always_proceed(self, tmp_path):
        """glob (read) should always be PROCEED."""
        engine = FoolProofEngine(tmp_path)
        check = engine.evaluate("glob", {"pattern": "**/*.py"})
        assert check.action == ActionRequired.PROCEED

    def test_write_file_requires_confirm(self, tmp_path):
        """write_file without permissions should be CONFIRM."""
        engine = FoolProofEngine(tmp_path)
        check = engine.evaluate("write_file", {"file_path": "test.txt", "content": "hi"})
        assert check.allowed is True
        assert check.action in (ActionRequired.CONFIRM, ActionRequired.WARN_CONFIRM)

    def test_shell_requires_confirm(self, tmp_path):
        """run_shell without permissions should be CONFIRM."""
        engine = FoolProofEngine(tmp_path)
        check = engine.evaluate("run_shell", {"command": "ls -la"})
        assert check.action in (ActionRequired.CONFIRM, ActionRequired.WARN_CONFIRM)

    def test_dangerous_shell_command_warn_confirm(self, tmp_path):
        """Dangerous shell commands should be WARN_CONFIRM."""
        engine = FoolProofEngine(tmp_path)
        check = engine.evaluate("run_shell", {"command": "git push --force"})
        assert check.action == ActionRequired.WARN_CONFIRM

    def test_critical_shell_blocked(self, tmp_path):
        """Critical commands like rm -rf / should be BLOCKED."""
        engine = FoolProofEngine(tmp_path)
        check = engine.evaluate("run_shell", {"command": "rm -rf /"})
        assert check.allowed is False
        assert check.action == ActionRequired.BLOCKED

    def test_permission_allow_bypasses_confirm(self, tmp_path):
        """Permissions with ALLOW should bypass confirmation."""
        permissions = PermissionStore()
        permissions.set_category_rule("shell", PermissionMode.ALLOW)
        engine = FoolProofEngine(tmp_path, permission_store=permissions)
        check = engine.evaluate("run_shell", {"command": "ls"})
        assert check.action == ActionRequired.PROCEED

    def test_permission_deny_blocks(self, tmp_path):
        """Permissions with DENY should block."""
        permissions = PermissionStore()
        permissions.set_category_rule("write", PermissionMode.DENY)
        engine = FoolProofEngine(tmp_path, permission_store=permissions)
        check = engine.evaluate("write_file", {"file_path": "test.txt", "content": "hi"})
        assert check.allowed is False
        assert check.action == ActionRequired.BLOCKED

    def test_mcp_tool_caution(self, tmp_path):
        """MCP tools should be CAUTION with a warning."""
        engine = FoolProofEngine(tmp_path)
        check = engine.evaluate("mcp__read_database", {"query": "SELECT *"})
        assert check.allowed is True
        assert check.risk == RiskLevel.CAUTION

    def test_unknown_tool_caution(self, tmp_path):
        """Unknown tools should get CAUTION with a warning."""
        engine = FoolProofEngine(tmp_path)
        check = engine.evaluate("some_unknown_tool", {})
        assert check.allowed is True
        assert check.risk == RiskLevel.CAUTION
        assert any("Unknown tool" in w for w in check.warnings)

    def test_evaluate_danger_message_format(self, tmp_path):
        """Danger messages should include tool name and command."""
        engine = FoolProofEngine(tmp_path)
        check = engine.evaluate("run_shell", {"command": "git push --force"})
        assert "[DANGER]" in check.confirm_message
        assert "git push --force" in check.confirm_message


class TestFoolProofEngineCapture:
    """FoolProofEngine.capture — post-execution tracking."""

    def test_capture_write_with_tracker(self, tmp_path):
        """Capturing a write should record in change tracker."""
        tracker = ChangeTracker("test", backup_dir=tmp_path / "backups")
        engine = FoolProofEngine(tmp_path, change_tracker=tracker)

        test_file = tmp_path / "capture_test.txt"
        change = engine.capture("write_file", {"file_path": str(test_file), "content": "hi"}, result=None)
        assert change is not None
        assert tracker.count_active() == 1

    def test_capture_without_tracker(self, tmp_path):
        """Capturing without a tracker should return None."""
        engine = FoolProofEngine(tmp_path)
        result = engine.capture("write_file", {"file_path": "test.txt", "content": "hi"}, result=None)
        assert result is None

    def test_capture_edit_with_tracker(self, tmp_path):
        """Capturing an edit should record in change tracker."""
        tracker = ChangeTracker("test", backup_dir=tmp_path / "backups")
        engine = FoolProofEngine(tmp_path, change_tracker=tracker)

        test_file = tmp_path / "edit_capture.txt"
        test_file.write_text("original")
        # For edit_file to capture, the file must be written with new content first
        test_file.write_text("modified")
        change = engine.capture("edit_file", {"file_path": str(test_file)}, result=None, old_content="original")
        assert change is not None


class TestFoolProofEngineDryRun:
    """Dry-run preview generation."""

    def test_dry_run_write_preview(self, tmp_path):
        """With dry-run, evaluate should include preview text."""
        tracker = ChangeTracker("test", backup_dir=tmp_path / "backups")
        tracker.dry_run = True
        engine = FoolProofEngine(tmp_path, change_tracker=tracker)

        check = engine.evaluate("write_file", {
            "file_path": "test.txt", "content": "line1\nline2\n",
        })
        assert "Would WRITE" in check.dry_run_preview

    def test_dry_run_edit_preview(self, tmp_path):
        """With dry-run, edit preview should show old/new."""
        tracker = ChangeTracker("test", backup_dir=tmp_path / "backups")
        tracker.dry_run = True
        engine = FoolProofEngine(tmp_path, change_tracker=tracker)

        check = engine.evaluate("edit_file", {
            "file_path": "test.txt",
            "old_string": "original",
            "new_string": "modified",
        })
        assert "Would EDIT" in check.dry_run_preview
        assert "original" in check.dry_run_preview
        assert "modified" in check.dry_run_preview

    def test_dry_run_shell_preview(self, tmp_path):
        """With dry-run, shell preview should show command."""
        tracker = ChangeTracker("test", backup_dir=tmp_path / "backups")
        tracker.dry_run = True
        engine = FoolProofEngine(tmp_path, change_tracker=tracker)

        check = engine.evaluate("run_shell", {"command": "ls -la"})
        assert "Would RUN" in check.dry_run_preview

    def test_dry_run_unknown_tool(self, tmp_path):
        """Unknown tool in dry-run should return empty preview."""
        tracker = ChangeTracker("test", backup_dir=tmp_path / "backups")
        tracker.dry_run = True
        engine = FoolProofEngine(tmp_path, change_tracker=tracker)

        check = engine.evaluate("unknown_tool", {})
        assert check.dry_run_preview == ""


class TestFoolProofEngineStats:
    """FoolProofEngine stats tracking."""

    def test_stats_initial(self, tmp_path):
        """Initial stats should be zero."""
        engine = FoolProofEngine(tmp_path)
        stats = engine.stats
        assert stats["blocks"] == 0
        assert stats["confirmations"] == 0

    def test_stats_block_count(self, tmp_path):
        """Blocked actions should increment block count."""
        engine = FoolProofEngine(tmp_path)
        engine.evaluate("run_shell", {"command": "rm -rf /"})
        assert engine.stats["blocks"] == 1

    def test_stats_confirmation_count(self, tmp_path):
        """Actions requiring confirm should increment confirmation count."""
        engine = FoolProofEngine(tmp_path)
        engine.evaluate("write_file", {"file_path": "test.txt", "content": "hi"})
        assert engine.stats["confirmations"] >= 1

    def test_stats_with_tracker(self, tmp_path):
        """Stats should include tracker counts when tracker is present."""
        tracker = ChangeTracker("test", backup_dir=tmp_path / "backups")
        engine = FoolProofEngine(tmp_path, change_tracker=tracker)
        engine.capture("write_file", {"file_path": "test.txt", "content": "hi"}, result=None)
        stats = engine.stats
        assert stats["tracker_changes"] == 1
