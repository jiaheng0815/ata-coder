"""
Self-Correction Loop — automatic error recovery.

When a tool returns an error, the agent:
1. Reads the error message
2. Diagnoses the root cause (common patterns)
3. Suggests a fix
4. Retries with corrected arguments (max 3 attempts)
5. Learns from failures within the session

Common error patterns detected:
- File not found → suggest reading directory first
- old_string not found → suggest reading file again
- Command not found → suggest alternative or install
- Permission denied → suggest elevated privileges
- Syntax error → suggest reading linter output
- Import error → suggest installing missing package
- Network error → suggest retry or check connectivity
"""

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Error pattern → diagnosis + fix suggestion
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class ErrorDiagnosis:
    pattern: str
    diagnosis: str
    fix_suggestion: str
    retry_strategy: str  # "auto_fix" | "read_first" | "ask_user" | "skip"


# Common error patterns with auto-fix strategies
ERROR_PATTERNS: list[ErrorDiagnosis] = [
    ErrorDiagnosis(
        pattern=r"old_string not found in file",
        diagnosis="The file has changed since it was last read. The edit target no longer matches.",
        fix_suggestion="Re-read the file to get current contents, then re-apply the edit.",
        retry_strategy="read_first",
    ),
    ErrorDiagnosis(
        pattern=r"File not found",
        diagnosis="The specified file does not exist at the given path.",
        fix_suggestion="Check the file path. List the directory to find the correct path, or create the file first.",
        retry_strategy="read_first",
    ),
    ErrorDiagnosis(
        pattern=r"Permission denied|EACCES|Access is denied",
        diagnosis="The current user does not have permission to access this file or run this command.",
        fix_suggestion="Check file permissions. Consider using elevated privileges or choosing a different path.",
        retry_strategy="ask_user",
    ),
    ErrorDiagnosis(
        pattern=r"command not found|not recognized|No such file or directory",
        diagnosis="The shell command is not available on this system.",
        fix_suggestion="Check the command name. Consider installing the required tool or using an alternative.",
        retry_strategy="ask_user",
    ),
    ErrorDiagnosis(
        pattern=r"SyntaxError|syntax error|invalid syntax|unexpected token",
        diagnosis="There is a syntax error in the code.",
        fix_suggestion="Review the syntax. Check for missing brackets, quotes, colons, or incorrect indentation.",
        retry_strategy="auto_fix",
    ),
    ErrorDiagnosis(
        pattern=r"ModuleNotFoundError|ImportError|No module named",
        diagnosis="A required Python module is not installed.",
        fix_suggestion="Install the missing module with pip, or check the import path.",
        retry_strategy="auto_fix",
    ),
    ErrorDiagnosis(
        pattern=r"undefined|is not defined|ReferenceError|NameError",
        diagnosis="A variable or function is being used before it is defined.",
        fix_suggestion="Check the variable/function name for typos. Ensure it is defined before use.",
        retry_strategy="auto_fix",
    ),
    ErrorDiagnosis(
        pattern=r"TypeError|TypeError:|wrong type|expected .* but got",
        diagnosis="A function received an argument of the wrong type.",
        fix_suggestion="Check the types of arguments being passed. Add type conversions if needed.",
        retry_strategy="auto_fix",
    ),
    ErrorDiagnosis(
        pattern=r"timeout|timed out|Timed out",
        diagnosis="The operation took too long and timed out.",
        fix_suggestion="Increase the timeout, break the task into smaller pieces, or check network connectivity.",
        retry_strategy="auto_fix",
    ),
    ErrorDiagnosis(
        pattern=r"out of memory|MemoryError|OOM",
        diagnosis="The operation ran out of memory.",
        fix_suggestion="Process data in smaller batches. Close unused resources. Consider streaming.",
        retry_strategy="ask_user",
    ),
    ErrorDiagnosis(
        pattern=r"rate limit|too many requests|429",
        diagnosis="API rate limit has been exceeded.",
        fix_suggestion="Wait and retry with exponential backoff. The client already handles this automatically.",
        retry_strategy="auto_fix",
    ),
    ErrorDiagnosis(
        pattern=r"connection refused|ConnectionError|NetworkError|could not connect",
        diagnosis="Cannot connect to the remote service.",
        fix_suggestion="Check that the service is running. Verify the URL and port. Check network/firewall.",
        retry_strategy="ask_user",
    ),
]


# ═══════════════════════════════════════════════════════════════════════════════
# Retry tracker
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class RetryAttempt:
    attempt: int
    tool_name: str
    original_args: dict
    error_message: str
    diagnosis: ErrorDiagnosis | None
    fixed_args: dict | None
    success: bool = False
    timestamp: float = 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# Self-Correction Engine
# ═══════════════════════════════════════════════════════════════════════════════

class SelfCorrectionEngine:
    """
    Automatically diagnoses and recovers from tool execution errors.

    Usage:
        engine = SelfCorrectionEngine(max_retries=3)

        result = execute_tool(name, args)
        if not result.success:
            diagnosis = engine.diagnose(result.error)
            if diagnosis.retry_strategy == "auto_fix":
                fixed_args = engine.suggest_fix(name, args, diagnosis)
                result = execute_tool(name, fixed_args)  # retry
    """

    def __init__(self, max_retries: int = 3):
        self.max_retries = max_retries
        self._attempts: list[RetryAttempt] = []
        self._learned_patterns: dict[str, str] = {}  # session learning

    # ── Diagnosis ────────────────────────────────────────────────────────

    def diagnose(self, error_message: str, tool_name: str = "",
                  arguments: dict | None = None) -> ErrorDiagnosis | None:
        """
        Analyze an error message and return a diagnosis with fix strategy.
        """
        # Check known patterns
        for pattern in ERROR_PATTERNS:
            if re.search(pattern.pattern, error_message, re.IGNORECASE):
                logger.info("Diagnosed: %s → %s", pattern.pattern[:40], pattern.retry_strategy)
                return pattern

        # Check learned patterns from this session
        for learned_pattern, fix in self._learned_patterns.items():
            if re.search(learned_pattern, error_message, re.IGNORECASE):
                return ErrorDiagnosis(
                    pattern=learned_pattern,
                    diagnosis="Learned from previous failure in this session",
                    fix_suggestion=fix,
                    retry_strategy="auto_fix",
                )

        # Unknown error — generic diagnosis
        return ErrorDiagnosis(
            pattern="unknown",
            diagnosis=f"Unexpected error during {tool_name}",
            fix_suggestion="Read the error carefully. Check the file, path, and arguments. Try a different approach.",
            retry_strategy="ask_user",
        )

    # ── Fix suggestion ──────────────────────────────────────────────────

    def suggest_fix(self, tool_name: str, arguments: dict,
                    diagnosis: ErrorDiagnosis) -> dict | None:
        """
        Suggest corrected arguments based on the diagnosis.
        Returns modified arguments dict, or None if no auto-fix is possible.
        """
        if diagnosis.retry_strategy == "ask_user":
            return None

        fixed = dict(arguments)

        if tool_name == "edit_file":
            if "old_string not found" in diagnosis.pattern:
                # Can't auto-fix without re-reading the file
                return None

        elif tool_name == "read_file":
            if "File not found" in diagnosis.pattern:
                # Try without extension, try src/ prefix
                fp = arguments.get("file_path", "")
                if fp and "." in fp:
                    # Try listing directory to find the file
                    return None  # need to read directory first

        elif tool_name == "run_shell":
            cmd = arguments.get("command", "")
            if "ModuleNotFoundError" in diagnosis.pattern or "No module named" in diagnosis.pattern:
                # Extract module name and suggest pip install
                match = re.search(r"No module named '(\w+)'", diagnosis.pattern)
                if not match:
                    match = re.search(r"No module named '(\w+)'", cmd)
                if match:
                    module = match.group(1)
                    fixed["command"] = f"pip install {module} && {cmd}"
                    return fixed

            if "command not found" in diagnosis.pattern:
                # Try with python -m prefix
                first_word = cmd.strip().split()[0] if cmd.strip() else ""
                if first_word and not first_word.startswith("python"):
                    fixed["command"] = f"python -m {cmd}"
                    return fixed

            if "timeout" in diagnosis.pattern:
                # Increase timeout
                fixed["timeout"] = arguments.get("timeout", 120) * 2
                return fixed

        return fixed  # return original args for retry

    # ── Retry management ────────────────────────────────────────────────

    def should_retry(self, tool_name: str, arguments: dict) -> bool:
        """Check if we should retry this tool call."""
        count = sum(
            1 for a in self._attempts
            if a.tool_name == tool_name
            and a.original_args == arguments
        )
        return count < self.max_retries

    def record_attempt(self, tool_name: str, original_args: dict,
                       error: str, diagnosis: ErrorDiagnosis | None,
                       fixed_args: dict | None, success: bool = False):
        """Record a retry attempt for tracking."""
        attempt = RetryAttempt(
            attempt=len(self._attempts) + 1,
            tool_name=tool_name,
            original_args=original_args,
            error_message=error,
            diagnosis=diagnosis,
            fixed_args=fixed_args,
            success=success,
            timestamp=time.time(),
        )
        self._attempts.append(attempt)

    def learn_from_success(self, error_pattern: str, fix_description: str):
        """Learn a fix pattern from a successful correction."""
        self._learned_patterns[error_pattern] = fix_description
        logger.info("Learned fix: %s", fix_description[:80])

    # ── Auto-correction loop ─────────────────────────────────────────────

    def auto_correct(
        self,
        tool_name: str,
        arguments: dict,
        error_message: str,
        execute_fn: Callable[[str, dict], Any],
    ) -> tuple[Any, int]:
        """
        Run the full auto-correction loop.

        Args:
            tool_name: The tool that failed
            arguments: Original arguments
            error_message: The error from the failed execution
            execute_fn: Function to execute the tool (name, args) → result

        Returns:
            (result, retry_count): The final result and number of retries used
        """
        retries = 0

        while retries < self.max_retries:
            # Diagnose
            diagnosis = self.diagnose(error_message, tool_name, arguments)
            if not diagnosis:
                break

            # Check if auto-fixable
            if diagnosis.retry_strategy == "ask_user":
                logger.info("Cannot auto-fix: %s", diagnosis.diagnosis)
                break

            if diagnosis.retry_strategy == "read_first":
                logger.info("Need to read context first: %s", diagnosis.diagnosis)
                break

            # Try auto-fix
            fixed_args = self.suggest_fix(tool_name, arguments, diagnosis)
            retries += 1

            if fixed_args is None:
                break

            logger.info(
                "Auto-correct retry %d/%d: %s",
                retries, self.max_retries, diagnosis.fix_suggestion[:80],
            )

            # Execute with fixed args
            try:
                result = execute_fn(tool_name, fixed_args)
            except Exception as e:
                result = type('Result', (), {'success': False, 'error': str(e)})()

            self.record_attempt(
                tool_name, arguments, error_message,
                diagnosis, fixed_args, getattr(result, 'success', False),
            )

            if getattr(result, 'success', False):
                # Learn from this success
                pattern = re.escape(error_message[:80])
                self.learn_from_success(pattern, diagnosis.fix_suggestion)
                return result, retries

            # Update error for next iteration
            error_message = getattr(result, 'error', str(result))
            arguments = fixed_args

        return None, retries

    # ── Statistics ──────────────────────────────────────────────────────

    @property
    def stats(self) -> dict:
        total = len(self._attempts)
        successful = sum(1 for a in self._attempts if a.success)
        return {
            "total_retries": total,
            "successful_retries": successful,
            "learned_patterns": len(self._learned_patterns),
            "auto_fix_rate": f"{successful/max(1,total)*100:.0f}%",
        }

    def reset(self):
        self._attempts.clear()
        self._learned_patterns.clear()
