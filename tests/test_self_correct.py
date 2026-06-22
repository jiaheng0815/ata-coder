"""
Unit tests for self_correct — error diagnosis, fix suggestion, retry tracking,
session learning, and auto-correction loop.
"""
import pytest
from ata_coder.self_correct import (
    SelfCorrectionEngine,
    ErrorDiagnosis,
    RetryAttempt,
    ERROR_PATTERNS,
)


# ── Diagnosis: regex pattern matching ──────────────────────────────────────


class TestDiagnose:
    """diagnose matches error messages against known patterns."""

    def test_file_not_found(self):
        engine = SelfCorrectionEngine()
        d = engine.diagnose("File not found: /tmp/nope.py")
        assert d is not None
        assert d.retry_strategy == "read_first"

    def test_permission_denied(self):
        engine = SelfCorrectionEngine()
        d = engine.diagnose("Permission denied: /etc/shadow")
        assert d is not None
        assert d.retry_strategy == "ask_user"

    def test_command_not_found(self):
        engine = SelfCorrectionEngine()
        d = engine.diagnose("bash: foo: command not found")
        assert d is not None
        assert d.retry_strategy == "ask_user"

    def test_module_not_found(self):
        engine = SelfCorrectionEngine()
        d = engine.diagnose("ModuleNotFoundError: No module named 'requests'")
        assert d is not None
        assert d.retry_strategy == "auto_fix"

    def test_type_error(self):
        engine = SelfCorrectionEngine()
        d = engine.diagnose("TypeError: expected str but got int")
        assert d is not None
        assert d.retry_strategy == "auto_fix"

    def test_timeout(self):
        engine = SelfCorrectionEngine()
        d = engine.diagnose("Operation timed out after 30 seconds")
        assert d is not None
        assert d.retry_strategy == "auto_fix"

    def test_rate_limit(self):
        engine = SelfCorrectionEngine()
        d = engine.diagnose("HTTP 429: rate limit exceeded")
        assert d is not None
        assert d.retry_strategy == "auto_fix"

    def test_connection_refused(self):
        engine = SelfCorrectionEngine()
        d = engine.diagnose("Connection refused to localhost:8080")
        assert d is not None
        assert d.retry_strategy == "ask_user"

    def test_unknown_error_returns_generic(self):
        engine = SelfCorrectionEngine()
        d = engine.diagnose("some completely random error text")
        assert d is not None  # always returns a diagnosis
        assert d.retry_strategy == "ask_user"
        assert "Unexpected error" in d.diagnosis

    def test_learned_pattern_used(self):
        engine = SelfCorrectionEngine()
        engine.learn_from_success("random error XYZ123", "Restart the service")
        d = engine.diagnose("Got random error XYZ123 again")
        assert d is not None
        assert d.retry_strategy == "auto_fix"
        assert "Learned from previous" in d.diagnosis

    def test_case_insensitive_matching(self):
        engine = SelfCorrectionEngine()
        d = engine.diagnose("FILE NOT FOUND: /tmp/x")
        assert d is not None
        assert d.pattern == "File not found"


# ── Fix suggestion: argument correction ────────────────────────────────────


class TestSuggestFix:
    """suggest_fix produces corrected arguments based on diagnosis."""

    def test_ask_user_returns_none(self):
        engine = SelfCorrectionEngine()
        d = ErrorDiagnosis("x", "y", "z", "ask_user")
        result = engine.suggest_fix("read_file", {"file_path": "/x"}, d)
        assert result is None

    def test_read_file_not_found_returns_none(self):
        """read_file can't auto-fix missing files."""
        engine = SelfCorrectionEngine()
        d = engine.diagnose("File not found: /tmp/x.py")
        result = engine.suggest_fix("read_file", {"file_path": "/tmp/x.py"}, d)
        assert result is None

    def test_run_shell_pip_install_prepended(self):
        engine = SelfCorrectionEngine()
        d = engine.diagnose("ModuleNotFoundError: No module named 'requests'")
        result = engine.suggest_fix(
            "run_shell", {"command": "python -c 'import requests'"},
            d, error_message="ModuleNotFoundError: No module named 'requests'"
        )
        assert result is not None
        assert "pip install requests" in result["command"]
        assert result["_original_command"] == "python -c 'import requests'"

    def test_run_shell_pip_install_idempotent(self):
        """Don't double-prepend pip install when command already starts with it."""
        engine = SelfCorrectionEngine()
        d = engine.diagnose("ModuleNotFoundError: No module named 'requests'")
        # Command already has pip install (no _original_command set)
        result = engine.suggest_fix(
            "run_shell",
            {"command": "pip install requests && python main.py"},
            d, error_message="No module named 'requests'"
        )
        # orig_cmd == cmd (no _original_command), starts with "pip install" → None
        assert result is None

    def test_run_shell_command_not_found_no_auto_fix(self):
        """command-not-found has retry_strategy='ask_user', so suggest_fix
        returns None before reaching the python -m fallback.  This is by
        design — the engine asks the user before trying python -m."""
        engine = SelfCorrectionEngine()
        d = engine.diagnose("pytest: command not found")
        assert d.retry_strategy == "ask_user"
        result = engine.suggest_fix("run_shell", {"command": "pytest -v"}, d)
        assert result is None

    def test_run_shell_command_not_found_non_python(self):
        """Non-Python tools (git, npm) can't be auto-fixed."""
        engine = SelfCorrectionEngine()
        d = engine.diagnose("git: command not found")
        result = engine.suggest_fix("run_shell", {"command": "git status"}, d)
        assert result is None

    def test_run_shell_timeout_doubled(self):
        engine = SelfCorrectionEngine()
        d = engine.diagnose("Operation timed out")
        result = engine.suggest_fix("run_shell", {"command": "sleep 100", "timeout": 30}, d)
        assert result is not None
        assert result["timeout"] == 60

    def test_no_change_no_retry(self):
        """If fix doesn't change arguments, return None to skip retry."""
        engine = SelfCorrectionEngine()
        d = engine.diagnose("TypeError: wrong type")
        result = engine.suggest_fix("unknown_tool", {"arg": "val"}, d)
        # fixed == arguments, so returns None
        assert result is None


# ── Retry tracking ─────────────────────────────────────────────────────────


class TestRetryTracking:
    """should_retry and record_attempt manage retry limits."""

    def test_should_retry_within_limit(self):
        engine = SelfCorrectionEngine(max_retries=3)
        assert engine.should_retry("read_file", {"file_path": "/x"}) is True

    def test_should_not_retry_exceeded(self):
        engine = SelfCorrectionEngine(max_retries=1)
        engine.record_attempt("read_file", {"file_path": "/x"}, "err", None, None)
        assert engine.should_retry("read_file", {"file_path": "/x"}) is False

    def test_record_attempt_increments(self):
        engine = SelfCorrectionEngine()
        engine.record_attempt("grep", {"pattern": "x"}, "error",
                              ErrorDiagnosis("p", "d", "f", "auto_fix"),
                              {"pattern": "y"}, success=True)
        assert len(engine._attempts) == 1
        assert engine._attempts[0].success is True
        assert engine._attempts[0].tool_name == "grep"

    def test_different_args_independent_retries(self):
        engine = SelfCorrectionEngine(max_retries=1)
        engine.record_attempt("read_file", {"file_path": "/a"}, "err", None, None)
        # Different args → different retry count
        assert engine.should_retry("read_file", {"file_path": "/b"}) is True


# ── Session learning ───────────────────────────────────────────────────────


class TestSessionLearning:
    """learn_from_success populates session-level fix cache."""

    def test_learn_and_apply(self):
        engine = SelfCorrectionEngine()
        engine.learn_from_success("specific error ABC", "Run setup.sh first")
        assert len(engine._learned_patterns) == 1

    def test_stats_reflects_learning(self):
        engine = SelfCorrectionEngine()
        engine.learn_from_success("err1", "fix1")
        engine.learn_from_success("err2", "fix2")
        assert engine.stats["learned_patterns"] == 2

    def test_reset_clears_all(self):
        engine = SelfCorrectionEngine()
        engine.learn_from_success("err", "fix")
        engine.record_attempt("tool", {}, "err", None, None, success=True)
        engine.reset()
        assert len(engine._attempts) == 0
        assert len(engine._learned_patterns) == 0


# ── Edge cases ─────────────────────────────────────────────────────────────


def test_empty_error_message():
    """Empty error should return generic diagnosis, not crash."""
    engine = SelfCorrectionEngine()
    d = engine.diagnose("")
    assert d is not None
    assert d.retry_strategy == "ask_user"


def test_stats_initial_state():
    engine = SelfCorrectionEngine()
    s = engine.stats
    assert s["total_retries"] == 0
    assert s["successful_retries"] == 0
    assert s["learned_patterns"] == 0
    # No division by zero on empty attempts
    assert s["auto_fix_rate"] in ("0%", "0%")


def test_error_patterns_not_empty():
    """Sanity check: ERROR_PATTERNS list has entries."""
    assert len(ERROR_PATTERNS) >= 8
    # All entries must have valid retry_strategy values
    valid_strategies = {"auto_fix", "read_first", "ask_user", "skip"}
    for ep in ERROR_PATTERNS:
        assert ep.retry_strategy in valid_strategies
