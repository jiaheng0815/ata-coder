"""
Safety Guard — prevents dangerous operations before they happen.

Four risk levels:
  SAFE     — Read-only operations, always allowed
  CAUTION  — Write/modify within workspace, ask once
  DANGER   — Shell commands, file deletes outside .git, warn strongly
  CRITICAL — Destructive system commands, require explicit typing to confirm

Guard rails:
- Path traversal detection (../../etc/passwd)
- Workspace boundary enforcement
- Command injection patterns (piped dangerous commands)
- File type restrictions (.exe, .dll write prevention)
- Sensitive path protection (/etc, C:/Windows, ~/.ssh)
- Max file size limit (prevent writing giant files)
- Recursive delete detection
- Git force push prevention
"""

import logging
import os
import re
import shlex
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Risk levels
# ═══════════════════════════════════════════════════════════════════════════════

class RiskLevel(Enum):
    SAFE = 0
    CAUTION = 1
    DANGER = 2
    CRITICAL = 3

    def __lt__(self, other: "RiskLevel") -> bool:
        return self.value < other.value

    @property
    def key(self) -> str:
        return {RiskLevel.SAFE: "safe", RiskLevel.CAUTION: "caution",
                RiskLevel.DANGER: "danger", RiskLevel.CRITICAL: "critical"}[self]

    @property
    def label(self) -> str:
        return {RiskLevel.SAFE: "SAFE", RiskLevel.CAUTION: "CAUTION",
                RiskLevel.DANGER: "DANGER", RiskLevel.CRITICAL: "CRITICAL"}[self]

    @property
    def color(self) -> str:
        return {RiskLevel.SAFE: "green", RiskLevel.CAUTION: "yellow",
                RiskLevel.DANGER: "red", RiskLevel.CRITICAL: "bold red"}[self]

    @property
    def confirm(self) -> bool:
        return self != RiskLevel.SAFE


# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class SafetyCheck:
    """Result of a safety check."""
    allowed: bool
    risk: RiskLevel
    reason: str = ""
    warnings: list[str] = field(default_factory=list)
    requires_typing: bool = False  # user must type "yes i understand" to proceed


# ═══════════════════════════════════════════════════════════════════════════════
# Protected paths & patterns
# ═══════════════════════════════════════════════════════════════════════════════

# Paths that should never be written to
PROTECTED_PATHS = [
    # Unix
    "/etc/", "/boot/", "/sys/", "/proc/", "/dev/",
    "/root/", "/var/log/", "/var/run/",
    "~/.ssh/", "~/.gnupg/", "~/.aws/", "~/.config/",
    # Windows
    "C:\\Windows\\", "C:\\Windows\\System32\\",
    "C:\\Program Files\\", "C:\\Program Files (x86)\\",
    "%SystemRoot%\\", "%ProgramFiles%\\",
    # General
    ".git/HEAD", ".git/config", ".git/index",
    ".svn/", ".hg/",
]

# Destructive shell command patterns
DESTRUCTIVE_PATTERNS = [
    # System destruction
    (r"rm\s+-rf\s+/", RiskLevel.CRITICAL, "Recursive delete of root filesystem"),
    (r"rm\s+-rf\s+~", RiskLevel.CRITICAL, "Recursive delete of home directory"),
    (r"rm\s+-rf\s+\$HOME", RiskLevel.CRITICAL, "Recursive delete of home directory"),
    (r"mkfs\.", RiskLevel.CRITICAL, "Filesystem format"),
    (r"dd\s+if=", RiskLevel.CRITICAL, "Raw disk write"),
    (r">\s*/dev/sd", RiskLevel.CRITICAL, "Direct disk write"),
    (r">\s*/dev/nvme", RiskLevel.CRITICAL, "Direct NVMe write"),
    (r"chmod\s+777\s+/", RiskLevel.CRITICAL, "World-writable root"),
    (r"chmod\s+-R\s+777\s+/", RiskLevel.CRITICAL, "World-writable root recursive"),

    # System control
    (r"shutdown", RiskLevel.DANGER, "System shutdown"),
    (r"reboot", RiskLevel.DANGER, "System reboot"),
    (r"systemctl\s+stop", RiskLevel.DANGER, "Stop system service"),
    (r"systemctl\s+disable", RiskLevel.DANGER, "Disable system service"),
    (r"killall", RiskLevel.DANGER, "Kill all processes"),
    (r"pkill", RiskLevel.DANGER, "Kill processes by pattern"),

    # Git danger
    (r"git\s+push\s+--force", RiskLevel.DANGER, "Force push"),
    (r"git\s+push\s+-f", RiskLevel.DANGER, "Force push"),
    (r"git\s+reset\s+--hard", RiskLevel.DANGER, "Hard reset — loses changes"),
    (r"git\s+clean\s+-fdx", RiskLevel.DANGER, "Remove all untracked files"),

    # Network danger
    (r"curl.*\|\s*(ba)?sh", RiskLevel.DANGER, "Pipe curl to shell"),
    (r"wget.*\|\s*(ba)?sh", RiskLevel.DANGER, "Pipe wget to shell"),
    (r"nc\s+-l", RiskLevel.CAUTION, "Open network listener"),

    # Fork bomb
    (r":\(\)\s*\{", RiskLevel.CRITICAL, "Fork bomb pattern"),

    # Database danger
    (r"DROP\s+(TABLE|DATABASE)", RiskLevel.DANGER, "SQL DROP operation"),
    (r"TRUNCATE\s+(TABLE\s+)?", RiskLevel.DANGER, "SQL TRUNCATE operation"),
    (r"DELETE\s+FROM\s+\w+\s+WHERE", RiskLevel.CAUTION, "SQL DELETE with condition"),
    (r"DELETE\s+FROM\s+\w+\s*;", RiskLevel.DANGER, "SQL DELETE without WHERE"),

    # Package manager danger
    (r"(pip|npm|gem|cargo)\s+(uninstall|remove)", RiskLevel.CAUTION, "Package removal"),

    # Permission changes
    (r"chmod\s+777", RiskLevel.CAUTION, "Make file world-writable"),
    (r"chown\s+root", RiskLevel.DANGER, "Change owner to root"),
]

# Suspicious file extensions (writing these is unusual for a code agent)
SUSPICIOUS_EXTENSIONS = {".exe", ".dll", ".so", ".dylib", ".bin", ".sys", ".drv", ".ko"}


# ═══════════════════════════════════════════════════════════════════════════════
# Safety Guard
# ═══════════════════════════════════════════════════════════════════════════════

class SafetyGuard:
    """
    Validates tool operations before execution.
    Returns SafetyCheck with risk level and warnings.
    """

    def __init__(self, workspace_dir: str | Path | None = None):
        self.workspace = Path(workspace_dir).resolve() if workspace_dir else Path.cwd().resolve()
        self._blocked_count = 0
        self._warned_count = 0

    # ── Check methods (one per tool category) ────────────────────────────

    def check_read_file(self, file_path: str) -> SafetyCheck:
        """Reading files is always safe."""
        return SafetyCheck(allowed=True, risk=RiskLevel.SAFE)

    def check_write_file(self, file_path: str, content: str = "") -> SafetyCheck:
        """Check file write safety."""
        warnings = []

        # 1. Path traversal check
        traversal_check = self._check_path_traversal(file_path)
        if not traversal_check.allowed:
            return traversal_check

        # 2. Workspace boundary
        boundary_check = self._check_workspace_boundary(file_path)
        if boundary_check.risk == RiskLevel.CRITICAL:
            return boundary_check
        if boundary_check.warnings:
            warnings.extend(boundary_check.warnings)

        # 3. Protected path check
        protected_check = self._check_protected_path(file_path)
        if not protected_check.allowed:
            return protected_check

        # 4. Suspicious extension
        ext = os.path.splitext(file_path)[1].lower()
        if ext in SUSPICIOUS_EXTENSIONS:
            warnings.append(f"Writing binary file: {ext}")

        # 5. Max file size
        if len(content) > 10_000_000:  # 10MB
            return SafetyCheck(
                allowed=False, risk=RiskLevel.DANGER,
                reason=f"File too large ({len(content):,} bytes). Max 10MB."
            )

        if warnings:
            return SafetyCheck(
                allowed=True, risk=RiskLevel.CAUTION,
                warnings=warnings,
            )

        return SafetyCheck(allowed=True, risk=RiskLevel.CAUTION)

    def check_edit_file(self, file_path: str, old_string: str, new_string: str) -> SafetyCheck:
        """Check file edit safety."""
        # Same checks as write, plus verify old_string exists
        write_check = self.check_write_file(file_path, new_string)
        if not write_check.allowed:
            return write_check

        path = Path(file_path)
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    current = f.read()
                if old_string not in current:
                    return SafetyCheck(
                        allowed=False, risk=RiskLevel.CAUTION,
                        reason="old_string not found in file. The file may have changed since you last read it. Read the file again first.",
                    )
            except Exception:
                pass

        return write_check

    def check_shell(self, command: str) -> SafetyCheck:
        """Check shell command safety."""
        warnings = []
        highest_risk = RiskLevel.CAUTION
        critical_reasons = []

        cmd_clean = command.strip()

        # 1. Check for destructive patterns
        for pattern, risk, reason in DESTRUCTIVE_PATTERNS:
            if re.search(pattern, cmd_clean, re.IGNORECASE):
                if risk == RiskLevel.CRITICAL:
                    critical_reasons.append(reason)
                elif risk == RiskLevel.DANGER:
                    warnings.append(f"DANGER: {reason}")
                else:
                    warnings.append(f"Caution: {reason}")

                if risk > highest_risk:
                    highest_risk = risk

        # 2. Critical block
        if critical_reasons:
            return SafetyCheck(
                allowed=False,
                risk=RiskLevel.CRITICAL,
                reason="\n".join(critical_reasons),
                warnings=warnings,
                requires_typing=False,  # hard block, not even type-to-confirm
            )

        # 3. Check for pipe to shell
        if "|" in cmd_clean and any(
            s in cmd_clean.lower() for s in ("sh", "bash", "zsh", "fish")
        ):
            warnings.append("Pipe to shell detected. Verify the source.")

        # 4. Check working directory is within workspace
        for part in shlex.split(cmd_clean) if _can_shlex(cmd_clean) else []:
            if part.startswith("/") or part.startswith("~"):
                full = os.path.expanduser(part)
                if os.path.exists(full):
                    try:
                        Path(full).resolve().relative_to(self.workspace)
                    except ValueError:
                        warnings.append(f"Path outside workspace: {part}")

        if highest_risk == RiskLevel.CRITICAL:
            return SafetyCheck(
                allowed=False, risk=RiskLevel.CRITICAL,
                warnings=warnings,
            )

        return SafetyCheck(
            allowed=True,
            risk=highest_risk,
            warnings=warnings,
        )

    # ── Internal checks ──────────────────────────────────────────────────

    def _check_path_traversal(self, file_path: str) -> SafetyCheck:
        """Detect path traversal attacks."""
        # Check for null bytes
        if "\0" in file_path:
            return SafetyCheck(
                allowed=False, risk=RiskLevel.CRITICAL,
                reason="Null byte in path (possible path truncation attack)",
            )

        # Check for ../ patterns — 3+ is suspicious
        traversal_count = file_path.count("..")
        if traversal_count >= 3:
            return SafetyCheck(
                allowed=False, risk=RiskLevel.CRITICAL,
                reason=f"Path traversal blocked ({traversal_count} '..' patterns). Write within the workspace.",
            )

        # Resolve the actual path if possible
        try:
            resolved = str(Path(file_path).resolve())
            workspace_str = str(self.workspace)
            if os.path.isabs(file_path) and not resolved.startswith(workspace_str):
                # Absolute path outside workspace
                return SafetyCheck(
                    allowed=False, risk=RiskLevel.CRITICAL,
                    reason=f"Absolute path outside workspace: {file_path}",
                )
        except Exception:
            pass

        return SafetyCheck(allowed=True, risk=RiskLevel.SAFE)

    def _check_workspace_boundary(self, file_path: str) -> SafetyCheck:
        """Check if the path is within the workspace."""
        path = Path(file_path)
        if not path.is_absolute():
            path = self.workspace / path

        try:
            path.resolve().relative_to(self.workspace)
        except ValueError:
            # Path is outside workspace
            return SafetyCheck(
                allowed=True,  # not blocked, but strongly warned
                risk=RiskLevel.DANGER,
                warnings=[f"FILE OUTSIDE WORKSPACE: {file_path}\n  Workspace: {self.workspace}\n  Target: {path.resolve()}"],
            )

        return SafetyCheck(allowed=True, risk=RiskLevel.SAFE)

    def _check_protected_path(self, file_path: str) -> SafetyCheck:
        """Check if the path targets a system-protected location."""
        # Normalize the path
        normalized = os.path.normpath(file_path).replace("\\", "/")
        expanded = os.path.expanduser(normalized)

        # Try to resolve to actual path
        try:
            resolved = str(Path(expanded).resolve()).replace("\\", "/")
        except Exception:
            resolved = expanded

        for protected in PROTECTED_PATHS:
            p = os.path.expanduser(protected).replace("\\", "/")
            p_resolved = p
            try:
                p_resolved = str(Path(p).resolve()).replace("\\", "/")
            except Exception:
                pass

            # Match against expanded or resolved path
            if expanded.startswith(p) or resolved.startswith(p_resolved):
                return SafetyCheck(
                    allowed=False,
                    risk=RiskLevel.CRITICAL,
                    reason=f"Path is protected: {protected}",
                )

        return SafetyCheck(allowed=True, risk=RiskLevel.SAFE)

    # ── Block logging ────────────────────────────────────────────────────

    @property
    def stats(self) -> dict:
        return {
            "blocked": self._blocked_count,
            "warned": self._warned_count,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _can_shlex(cmd: str) -> bool:
    """Check if a command can be safely split with shlex."""
    try:
        shlex.split(cmd)
        return True
    except ValueError:
        return False
