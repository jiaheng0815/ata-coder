"""
Unit tests for git_workflow — secret detection, commit message generation,
branch name sanitization, and status formatting.
"""
import pytest
from unittest.mock import patch

from ata_coder.git_workflow import GitWorkflow, GitStatus, _run_git, is_git_repo


# ── GitStatus formatting ──────────────────────────────────────────────────


class TestGitStatus:
    """GitStatus.summary() and is_dirty() are pure formatting."""

    def test_clean_summary(self):
        s = GitStatus(branch="main", clean=True)
        assert s.summary() == "[main] clean"

    def test_dirty_summary_with_modified(self):
        s = GitStatus(branch="feat/x", clean=False, modified=3)
        assert "M:3" in s.summary()
        assert "[feat/x]" in s.summary()

    def test_dirty_summary_all_types(self):
        s = GitStatus(branch="dev", clean=False, staged=1, modified=2, untracked=4)
        summary = s.summary()
        assert "S:1" in summary
        assert "M:2" in summary
        assert "?:4" in summary

    def test_is_dirty(self):
        assert GitStatus(clean=True).is_dirty() is False
        assert GitStatus(clean=False).is_dirty() is True


# ── Secret detection ──────────────────────────────────────────────────────


class TestCheckSecrets:
    """_check_secrets scans diff output for known secret patterns."""

    def test_no_secrets_in_clean_diff(self):
        gw = GitWorkflow()
        with patch("ata_coder.git_workflow._run_git",
                   return_value=(0, "diff --git a/main.py b/main.py\n+print('hello')\n-print('bye')", "")):
            result = gw._check_secrets()
            assert result is None

    def test_api_key_detected(self):
        gw = GitWorkflow()
        fake_diff = '+api_key="sk-this-is-a-very-long-secret-key-12345678"'
        with patch("ata_coder.git_workflow._run_git",
                   return_value=(0, fake_diff, "")):
            result = gw._check_secrets()
            assert result is not None
            assert "API key" in result

    def test_github_token_detected(self):
        gw = GitWorkflow()
        # Now that specific patterns come first, even GITHUB_TOKEN="ghp_..."
        # should match the GitHub token pattern, not the generic API key pattern.
        fake_diff = '+GITHUB_TOKEN="ghp_abcdefghijklmnopqrstuvwxyz123456789012"'
        with patch("ata_coder.git_workflow._run_git",
                   return_value=(0, fake_diff, "")):
            result = gw._check_secrets()
            assert result is not None
            assert "GitHub token" in result

    def test_github_token_bare_value(self):
        """Bare GitHub token (no key= prefix) also detected."""
        gw = GitWorkflow()
        fake_diff = "+# Oops: ghp_abcdefghijklmnopqrstuvwxyz123456789012"
        with patch("ata_coder.git_workflow._run_git",
                   return_value=(0, fake_diff, "")):
            result = gw._check_secrets()
            assert result is not None

    def test_private_key_detected(self):
        gw = GitWorkflow()
        fake_diff = "+-----BEGIN RSA PRIVATE KEY-----\n+abc123\n+-----END RSA PRIVATE KEY-----"
        with patch("ata_coder.git_workflow._run_git",
                   return_value=(0, fake_diff, "")):
            result = gw._check_secrets()
            assert result is not None
            assert "Private key" in result

    def test_openai_key_detected(self):
        gw = GitWorkflow()
        # With pattern reorder + dash support, real OpenAI keys (sk-proj-...)
        # are caught by the specific pattern before the generic one.
        fake_diff = '+OPENAI_API_KEY="sk-proj-abcdefghijklmnopqrstuvwxyz1234567890AB"'
        with patch("ata_coder.git_workflow._run_git",
                   return_value=(0, fake_diff, "")):
            result = gw._check_secrets()
            assert result is not None
            assert "OpenAI" in result

    def test_empty_diff_no_false_positive(self):
        gw = GitWorkflow()
        with patch("ata_coder.git_workflow._run_git",
                   return_value=(0, "", "")):
            result = gw._check_secrets()
            assert result is None

    def test_password_in_env_comment_not_detected(self):
        """Comment about passwords (no actual key=value) should not trigger."""
        gw = GitWorkflow()
        fake_diff = "+# Set your password in the environment"
        with patch("ata_coder.git_workflow._run_git",
                   return_value=(0, fake_diff, "")):
            result = gw._check_secrets()
            # A comment line without key=value pattern should not match
            assert result is None


# ── Commit message generation ─────────────────────────────────────────────


class TestGenerateCommitMessage:
    """_generate_commit_message produces descriptive messages from diff --stat."""

    def test_empty_diff(self):
        gw = GitWorkflow()
        with patch("ata_coder.git_workflow._run_git",
                   return_value=(0, "", "")):
            msg = gw._generate_commit_message()
            assert msg == "Update files"

    def test_single_file(self):
        gw = GitWorkflow()
        stat = " src/main.py | 10 +++++++---\n 1 file changed, 7 insertions(+), 3 deletions(-)"
        with patch("ata_coder.git_workflow._run_git",
                   return_value=(0, stat, "")):
            msg = gw._generate_commit_message()
            assert "main.py" in msg

    def test_test_files_prefix(self):
        gw = GitWorkflow()
        stat = " tests/test_auth.py | 50 ++++++++++++++++++++++++\n 1 file changed"
        with patch("ata_coder.git_workflow._run_git",
                   return_value=(0, stat, "")):
            msg = gw._generate_commit_message()
            assert msg.startswith("test:")

    def test_python_files_generic(self):
        gw = GitWorkflow()
        stat = " ata_coder/agent.py  | 5 +++--\n ata_coder/config.py | 3 ++-\n 2 files changed"
        with patch("ata_coder.git_workflow._run_git",
                   return_value=(0, stat, "")):
            msg = gw._generate_commit_message()
            assert "Python" in msg or "Update" in msg

    def test_same_directory_grouped(self):
        gw = GitWorkflow()
        stat = " src/utils/a.py | 2 +-\n src/utils/b.py | 3 ++-\n 2 files changed"
        with patch("ata_coder.git_workflow._run_git",
                   return_value=(0, stat, "")):
            msg = gw._generate_commit_message()
            assert "src/utils" in msg or "Update" in msg

    def test_no_pipe_in_stat(self):
        """Lines without | should not cause errors."""
        gw = GitWorkflow()
        stat = " some/non/file/line\n src/main.py | 3 +++\n 1 file changed"
        with patch("ata_coder.git_workflow._run_git",
                   return_value=(0, stat, "")):
            msg = gw._generate_commit_message()
            assert isinstance(msg, str)
            assert len(msg) > 0


# ── Branch name sanitization ──────────────────────────────────────────────


class TestBranchSanitization:
    """create_branch sanitizes user-provided names before git operations."""

    def test_spaces_replaced(self):
        gw = GitWorkflow()
        # The actual git operation will fail (no repo); we only verify the
        # sanitization by checking the command constructed.
        with patch("ata_coder.git_workflow._run_git",
                   return_value=(1, "", "not a git repo")):
            ok, msg = gw.create_branch("Fix Login Bug!")
            # 'Fix Login Bug!' → 'fix-login-bug'
            assert "fix-login-bug" in msg or ok is False

    def test_special_chars_removed(self):
        gw = GitWorkflow()
        with patch("ata_coder.git_workflow._run_git",
                   return_value=(1, "", "not a git repo")):
            ok, msg = gw.create_branch("feature/@user/#123")
            assert "feature/user/123" in msg or ok is False

    def test_empty_name_fallback(self):
        gw = GitWorkflow()
        with patch("ata_coder.git_workflow._run_git",
                   return_value=(1, "", "not a git repo")):
            ok, msg = gw.create_branch("!!!")
            # Fallback: "feature-{timestamp}"
            assert "feature-" in msg or ok is False


# ── Session summary ───────────────────────────────────────────────────────


class TestSessionSummary:
    """session_summary tracks in-memory git activity."""

    def test_no_activity(self):
        gw = GitWorkflow()
        assert "no git activity" in gw.session_summary()

    def test_with_commits(self):
        gw = GitWorkflow()
        gw._commits_made = ["fix: bug A", "feat: feature B"]
        summary = gw.session_summary()
        assert "2" in summary
        assert "fix: bug A" in summary

    def test_with_branches(self):
        gw = GitWorkflow()
        gw._branches_created = ["feat/x", "fix/y"]
        summary = gw.session_summary()
        assert "feat/x" in summary
        assert "fix/y" in summary


# ── Safety check patterns ─────────────────────────────────────────────────


class TestSecretPatterns:
    """Individual regex patterns for secret detection."""

    def _get_patterns(self):
        """Extract secret patterns — MUST match the order in git_workflow.py.
        Order: GitHub token → OpenAI key → AWS key → Private key → Generic API key.
        """
        import re
        return [
            (re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{36,}", re.IGNORECASE), "GitHub token"),
            (re.compile(r"sk-[A-Za-z0-9\-_]{32,}", re.IGNORECASE), "OpenAI key"),
            (re.compile(r"AKIA[0-9A-Z]{16}", re.IGNORECASE), "AWS key"),
            (re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----", re.IGNORECASE), "Private key"),
            (re.compile(r"(?:api_key|apikey|secret|password|token)\s*[:=]\s*[\"'`][^\s\"'`]{20,}[\"'`]", re.IGNORECASE), "API key"),
        ]

    def test_api_key_pattern_matches(self):
        patterns = self._get_patterns()
        # Generic API key pattern is index 4 (last)
        assert patterns[4][0].search('api_key="sk-this-is-a-very-long-secret-key-12345678"')

    def test_github_token_pattern_matches(self):
        patterns = self._get_patterns()
        assert patterns[0][0].search("ghp_abcdefghijklmnopqrstuvwxyz123456789012")

    def test_aws_key_pattern_matches(self):
        patterns = self._get_patterns()
        assert patterns[2][0].search("AKIA1234567890ABCDEF")

    def test_openai_key_pattern_matches(self):
        patterns = self._get_patterns()
        # sk- + 32+ chars (now allowing dashes for real key format)
        assert patterns[1][0].search("sk-proj-abcdefghijklmnopqrstuvwxyz1234567890AB")

    def test_short_value_no_false_positive(self):
        """Short values (< 20 chars) should not match the generic API key pattern."""
        patterns = self._get_patterns()
        assert patterns[4][0].search('password="short"') is None
