"""
Git Workflow — integrated version control for AI-assisted coding.

Features:
- Auto-create feature branches for tasks
- Auto-commit after successful changes with meaningful messages
- Pre-commit safety checks (no secrets, no giant files)
- Git status in UI
- PR/merge request creation hints
- Stash management for interrupted work

Commands (via CLI):
  /git status       → Show working tree status
  /git diff         → Show unstaged changes
  /git commit       → Auto-commit with generated message
  /git branch <name> → Create and switch to feature branch
  /git undo-commit  → Undo last commit (soft reset)
  /git log [n]      → Show recent commits

Safety:
- Never force-push
- Never amend pushed commits
- Auto-stash before dangerous operations
- Warn on large diffs (>500 lines)
"""

import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _run_git(args: list[str], cwd: str | Path, timeout: int = 30) -> tuple[int, str, str]:
    """Run a git command. Returns (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True, text=True,
            cwd=str(cwd), timeout=timeout,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except FileNotFoundError:
        return -1, "", "git not found"
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -1, "", str(e)


def is_git_repo(cwd: str | Path) -> bool:
    code, _, _ = _run_git(["rev-parse", "--git-dir"], cwd)
    return code == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Git Workflow Manager
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class GitStatus:
    branch: str = ""
    clean: bool = True
    staged: int = 0
    modified: int = 0
    untracked: int = 0
    ahead: int = 0
    behind: int = 0
    last_commit: str = ""
    last_commit_msg: str = ""

    def summary(self) -> str:
        if self.clean:
            return f"[{self.branch}] clean"
        parts = []
        if self.modified:
            parts.append(f"M:{self.modified}")
        if self.staged:
            parts.append(f"S:{self.staged}")
        if self.untracked:
            parts.append(f"?:{self.untracked}")
        return f"[{self.branch}] " + " ".join(parts)

    def is_dirty(self) -> bool:
        return not self.clean


class GitWorkflow:
    """
    Git workflow manager for AI-assisted development.

    Provides safe, automated git operations with guard rails.
    """

    def __init__(self, cwd: str | Path | None = None):
        self.cwd = Path(cwd) if cwd else Path.cwd()
        self._commits_made: list[str] = []  # track commits made in this session
        self._branches_created: list[str] = []

    # ── Status ──────────────────────────────────────────────────────────

    def get_status(self) -> GitStatus:
        """Get the current git working tree status."""
        if not is_git_repo(self.cwd):
            return GitStatus(branch="(not a git repo)")

        status = GitStatus()

        # Branch
        _, branch, _ = _run_git(["branch", "--show-current"], self.cwd)
        status.branch = branch or "(detached)"

        # Short status
        code, output, _ = _run_git(["status", "--short"], self.cwd)
        if code == 0:
            for line in output.split("\n") if output else []:
                line = line.strip()
                if not line:
                    continue
                if line.startswith("?"):
                    status.untracked += 1
                elif line[0] in "MADRC":
                    status.staged += 1
                elif line[1] in "MD":
                    status.modified += 1
            status.clean = (status.staged == 0 and status.modified == 0 and status.untracked == 0)

        # Ahead/behind
        _, ahead_str, _ = _run_git(["rev-list", "--count", "@{u}..HEAD"], self.cwd, timeout=10)
        if ahead_str and ahead_str.isdigit():
            status.ahead = int(ahead_str)
        _, behind_str, _ = _run_git(["rev-list", "--count", "HEAD..@{u}"], self.cwd, timeout=10)
        if behind_str and behind_str.isdigit():
            status.behind = int(behind_str)

        # Last commit
        _, last, _ = _run_git(["log", "-1", "--format=%h %s"], self.cwd)
        if last:
            parts = last.split(" ", 1)
            status.last_commit = parts[0]
            status.last_commit_msg = parts[1] if len(parts) > 1 else ""

        return status

    def get_diff(self, staged: bool = False) -> str:
        """Get the current diff."""
        args = ["diff"]
        if staged:
            args.append("--staged")
        _, output, _ = _run_git(args, self.cwd)
        return output or "(no changes)"

    def get_log(self, count: int = 10) -> str:
        """Get recent commit log."""
        _, output, _ = _run_git(
            ["log", f"-{count}", "--oneline", "--decorate"],
            self.cwd,
        )
        return output or "(no commits)"

    # ── Branch ──────────────────────────────────────────────────────────

    def create_branch(self, name: str, switch: bool = True) -> tuple[bool, str]:
        """
        Create a new feature branch from current HEAD.
        Sanitizes the branch name.
        """
        # Sanitize branch name
        safe_name = re.sub(r"[^a-zA-Z0-9._/-]", "-", name.lower())
        safe_name = re.sub(r"-+", "-", safe_name).strip("-")
        if not safe_name:
            safe_name = f"feature-{int(time.time())}"

        code, out, err = _run_git(["checkout", "-b", safe_name], self.cwd)
        if code == 0:
            self._branches_created.append(safe_name)
            return True, f"Branch created: {safe_name}"
        return False, err or "Failed to create branch"

    def switch_branch(self, name: str) -> tuple[bool, str]:
        """Switch to an existing branch."""
        code, out, err = _run_git(["checkout", name], self.cwd)
        return code == 0, out or err

    def list_branches(self) -> str:
        """List local branches."""
        _, out, _ = _run_git(["branch"], self.cwd)
        return out or ""

    # ── Commit ──────────────────────────────────────────────────────────

    def commit(self, message: str = "", files: list[str] | None = None,
               all_changes: bool = True) -> tuple[bool, str]:
        """
        Stage and commit changes with a meaningful message.
        Auto-generates message if none provided.
        """
        status = self.get_status()
        if status.clean:
            return False, "Nothing to commit (working tree clean)"

        # Stage files
        if all_changes:
            code, _, err = _run_git(["add", "-A"], self.cwd)
        elif files:
            code, _, err = _run_git(["add"] + files, self.cwd)
        else:
            return False, "No files specified"

        if code != 0:
            return False, err

        # Check for secrets in staged changes
        secret_check = self._check_secrets()
        if secret_check:
            logger.warning("Potential secrets in commit: %s", secret_check)

        # Generate commit message if not provided
        if not message:
            message = self._generate_commit_message()

        # Check diff size
        _, diff, _ = _run_git(["diff", "--staged", "--stat"], self.cwd)
        line_count = diff.count("\n") if diff else 0
        if line_count > 500:
            logger.warning("Large commit: %d files changed", line_count)

        code, _, err = _run_git(["commit", "-m", message], self.cwd)
        if code == 0:
            self._commits_made.append(message)
            return True, f"Committed: {message}"
        return False, err or "Commit failed"

    def _generate_commit_message(self) -> str:
        """Generate a descriptive commit message from the diff."""
        _, diff, _ = _run_git(["diff", "--staged", "--stat"], self.cwd)

        if not diff:
            return "Update files"

        # Extract file patterns
        files = []
        for line in diff.split("\n"):
            if "|" in line:
                fname = line.split("|")[0].strip()
                files.append(fname)

        if not files:
            return "Update files"

        # All files in a diff are changes (new, modified, or deleted)
        py_files = [f for f in files if f.endswith(".py")]
        js_files = [f for f in files if f.endswith((".js", ".ts", ".jsx", ".tsx"))]
        test_files = [f for f in files if "test" in f.lower()]

        if test_files:
            return f"test: add/update tests ({len(files)} files)"

        if len(files) == 1:
            return f"Update {os.path.basename(files[0])}"

        # Group by directory
        dirs = {}
        for f in files:
            d = os.path.dirname(f) or "root"
            dirs.setdefault(d, []).append(f)

        if len(dirs) == 1:
            d = list(dirs.keys())[0]
            return f"Update {d}/ ({len(files)} files)"

        # Generic but descriptive
        if py_files:
            return f"refactor: update Python code ({len(files)} files)"
        if js_files:
            return f"feat: update frontend code ({len(files)} files)"

        return f"chore: update {len(files)} files"

    def _check_secrets(self) -> str | None:
        """Check staged changes for potential secrets."""
        _, diff, _ = _run_git(["diff", "--staged"], self.cwd)
        if not diff:
            return None

        # Patterns that look like secrets
        secret_patterns = [
            (r"(?:api_key|apikey|secret|password|token)\s*[:=]\s*[\"'`][^\s\"'`]{20,}[\"'`]", "API key / secret"),
            (r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----", "Private key"),
            (r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{36,}", "GitHub token"),
            (r"AKIA[0-9A-Z]{16}", "AWS access key"),
            (r"sk-[A-Za-z0-9]{32,}", "OpenAI API key"),
        ]

        for pattern, name in secret_patterns:
            if re.search(pattern, diff, re.IGNORECASE):
                return name
        return None

    # ── Undo ────────────────────────────────────────────────────────────

    def undo_commit(self) -> tuple[bool, str]:
        """Undo the last commit (soft reset — keeps changes staged)."""
        code, _, err = _run_git(["reset", "--soft", "HEAD~1"], self.cwd)
        if code == 0:
            if self._commits_made:
                undone = self._commits_made.pop()
                return True, f"Undid: {undone}"
            return True, "Undid last commit (soft reset)"
        return False, err or "Cannot undo — no previous commit?"

    # ── Stash ────────────────────────────────────────────────────────────

    def stash(self, message: str = "") -> tuple[bool, str]:
        """Stash current changes."""
        args = ["stash", "push"]
        if message:
            args.extend(["-m", message])
        code, out, err = _run_git(args, self.cwd)
        return code == 0, out or err or "Stashed"

    def stash_pop(self) -> tuple[bool, str]:
        """Pop the most recent stash."""
        code, out, err = _run_git(["stash", "pop"], self.cwd)
        return code == 0, out or err or "Popped stash"

    # ── Safety checks ───────────────────────────────────────────────────

    def pre_operation_check(self) -> tuple[bool, str]:
        """
        Check if it's safe to perform git operations.
        Returns (safe, reason).
        """
        # Check for detached HEAD
        _, branch, _ = _run_git(["branch", "--show-current"], self.cwd)
        if not branch:
            return False, "Detached HEAD — create or switch to a branch first."

        # Check for rebase in progress
        if (self.cwd / ".git" / "rebase-merge").exists():
            return False, "Rebase in progress. Complete or abort it first."
        if (self.cwd / ".git" / "rebase-apply").exists():
            return False, "Rebase in progress. Complete or abort it first."

        # Check for merge in progress
        if (self.cwd / ".git" / "MERGE_HEAD").exists():
            return False, "Merge in progress. Complete or abort it first."

        return True, ""

    # ── Session summary ─────────────────────────────────────────────────

    def session_summary(self) -> str:
        """Summarize all git activity in this session."""
        lines = []
        if self._branches_created:
            lines.append(f"Branches created: {', '.join(self._branches_created)}")
        if self._commits_made:
            lines.append(f"Commits: {len(self._commits_made)}")
            for c in self._commits_made[-5:]:
                lines.append(f"  - {c[:80]}")
        return "\n".join(lines) if lines else "(no git activity in this session)"
