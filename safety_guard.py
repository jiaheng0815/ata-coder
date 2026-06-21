"""
Safety Guard — prevents dangerous operations before they happen.

⚠️ **Sync note**: This module has a TypeScript counterpart at
``ts-server/src/safety-guard.ts``.  Changes to risk levels, protected
paths, destructive patterns, or safety logic MUST be mirrored in both
files.  The Python version is the **source of truth** for safety rules;
the TS version replicates them for the companion server's standalone
validation (when the Python agent is not involved).

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


# ═══════════════════════════════════════════════════════════════════════════════
# Protected paths & patterns
# ═══════════════════════════════════════════════════════════════════════════════

# Paths that should never be written to
PROTECTED_PATHS = [
    # Unix
    "/etc/", "/boot/", "/sys/", "/proc/", "/dev/",
    "/root/", "/var/log/", "/var/run/",
    "~/.ssh/", "~/.gnupg/", "~/.aws/", "~/.config/",
    "~/.azure/", "~/.config/gcloud/", "~/.kube/",
    # Windows
    "C:\\Windows\\", "C:\\Windows\\System32\\",
    "C:\\Program Files\\", "C:\\Program Files (x86)\\",
    "%SystemRoot%\\", "%ProgramFiles%\\",
    "%APPDATA%\\Microsoft\\", "%USERPROFILE%\\.ssh\\",
    # General / dotfiles
    ".env", ".env.local", ".env.production",
    ".git/HEAD", ".git/config", ".git/index",
    ".svn/", ".hg/",
]

# Destructive shell command patterns
DESTRUCTIVE_PATTERNS = [
    # System destruction — single pattern covers /*, / *, /<wildcard> variants
    (r"\brm\s+.*-r\w*\s*-?f\w*\s+/(?:\s|\*|$)", RiskLevel.CRITICAL, "Recursive delete of root filesystem"),
    (r"\brm\s+.*-r\w*\s*-?f\w*\s+~", RiskLevel.CRITICAL, "Recursive delete of home directory"),
    (r"\brm\s+.*-r\w*\s*-?f\w*\s+\$HOME", RiskLevel.CRITICAL, "Recursive delete of home directory"),
    (r"\bfind\s+/.*-delete\b", RiskLevel.CRITICAL, "Recursive delete of root via find"),
    (r"\bfind\s+/\s+.*-delete\b", RiskLevel.CRITICAL, "Recursive delete of root via find"),
    (r"\bfind\s+/.*-exec\s+rm\b", RiskLevel.CRITICAL, "Recursive delete via find -exec rm"),
    (r"\bfind\s+/\s+.*-exec\s+rm\b", RiskLevel.CRITICAL, "Recursive delete of root via find -exec rm"),
    (r"mkfs\.", RiskLevel.CRITICAL, "Filesystem format"),
    (r"\bdd\s+if=", RiskLevel.CRITICAL, "Raw disk write (dd)"),
    (r"\bdd\s+of=", RiskLevel.CRITICAL, "Raw disk write (dd of=)"),
    (r">\s*/dev/sd", RiskLevel.CRITICAL, "Direct disk write"),
    (r">\s*/dev/nvme", RiskLevel.CRITICAL, "Direct NVMe write"),
    (r"\bshred\s+", RiskLevel.DANGER, "Secure file deletion"),
    # Command substitution — primary detection via _scan_command_substitutions()
    # which handles nested $(...); this regex is a fast-path catch for simple cases
    (r"\$\(.+\)", RiskLevel.CAUTION, "Command substitution detected"),
    (r"`[^`]+`", RiskLevel.CAUTION, "Backtick command substitution detected"),
    # $IFS bypass — shell expands $IFS to whitespace, evading space-delimited patterns
    (r"\$IFS|\$\{IFS\}", RiskLevel.CAUTION, "$IFS bypass — shell whitespace obfuscation"),
    # eval with command substitution — high-risk code execution chain
    (r"\beval\s+.*\$\(.+\)", RiskLevel.DANGER, "eval with command substitution"),
    (r"\beval\s+.*`[^`]+`", RiskLevel.DANGER, "eval with backtick substitution"),
    (r"chmod\s+777\s+/", RiskLevel.CRITICAL, "World-writable root"),
    (r"chmod\s+-R\s+777\s+/", RiskLevel.CRITICAL, "World-writable root recursive"),

    # System control
    (r"\bshutdown\b", RiskLevel.DANGER, "System shutdown"),
    (r"\breboot\b", RiskLevel.DANGER, "System reboot"),
    (r"\bsystemctl\s+stop\b", RiskLevel.DANGER, "Stop system service"),
    (r"\bsystemctl\s+disable\b", RiskLevel.DANGER, "Disable system service"),
    (r"\bkillall\b", RiskLevel.DANGER, "Kill all processes"),
    (r"\bpkill\b", RiskLevel.DANGER, "Kill processes by pattern"),

    # Git danger
    (r"\bgit\s+push\s+--force\b", RiskLevel.DANGER, "Force push"),
    (r"\bgit\s+push\s+-f\b", RiskLevel.DANGER, "Force push"),
    (r"\bgit\s+push\s+--force-with-lease\b", RiskLevel.DANGER, "Force push (with lease)"),
    (r"\bgit\s+push\s+[^ ]*\+", RiskLevel.DANGER, "Force push via +refspec"),
    (r"\bgit\s+reset\s+--hard\b", RiskLevel.DANGER, "Hard reset — loses changes"),
    (r"\bgit\s+clean\s+-fdx\b", RiskLevel.DANGER, "Remove all untracked files"),

    # Network danger
    (r"\bcurl\b.*\|\s*(ba)?sh\b", RiskLevel.DANGER, "Pipe curl to shell"),
    (r"\bwget\b.*\|\s*(ba)?sh\b", RiskLevel.DANGER, "Pipe wget to shell"),
    (r"\bnc\s+-l\b", RiskLevel.CAUTION, "Open network listener"),

    # Fork bomb
    (r":\(\)\s*\{", RiskLevel.CRITICAL, "Fork bomb pattern"),

    # Database danger
    (r"\bDROP\s+(TABLE|DATABASE)\b", RiskLevel.DANGER, "SQL DROP operation"),
    (r"\bTRUNCATE\s+(TABLE\s+)?", RiskLevel.DANGER, "SQL TRUNCATE operation"),
    (r"\bDELETE\s+FROM\s+\w+\s+WHERE\b", RiskLevel.CAUTION, "SQL DELETE with condition"),
    (r"\bDELETE\s+FROM\s+\w+\s*;", RiskLevel.DANGER, "SQL DELETE without WHERE"),

    # Package manager danger
    (r"\b(pip|npm|gem|cargo)\s+(uninstall|remove)\b", RiskLevel.CAUTION, "Package removal"),

    # Permission changes
    (r"\bchmod\s+777\b", RiskLevel.CAUTION, "Make file world-writable"),
    (r"\bchown\s+root\b", RiskLevel.DANGER, "Change owner to root"),

    # Encoded / obfuscated commands (common bypass techniques)
    (r"\bbase64\s+(-d|--decode)\b", RiskLevel.CAUTION, "Base64 decode — possible obfuscated command"),
    (r"\bIEX\s*\([^)]*\)", RiskLevel.DANGER, "PowerShell Invoke-Expression (IEX) — remote code execution risk"),
    (r"\bInvoke-Expression\b", RiskLevel.DANGER, "PowerShell Invoke-Expression — remote code execution risk"),
    (r"\bInvoke-WebRequest\b", RiskLevel.CAUTION, "PowerShell Invoke-WebRequest — fetches remote content"),
    (r"\bInvoke-RestMethod\b", RiskLevel.CAUTION, "PowerShell Invoke-RestMethod — fetches remote content"),
    # Additional PowerShell bypass techniques
    (r"\bStart-Process\s+.*-Verb\s+RunAs\b", RiskLevel.DANGER, "PowerShell elevated execution"),
    (r"\[\s*System\.Net\.WebClient\s*\]", RiskLevel.DANGER, "PowerShell WebClient — remote download"),
    (r"\[\s*System\.Reflection\.Assembly\s*\]", RiskLevel.DANGER, "PowerShell reflection assembly load"),

    # Additional bypass techniques
    (r"\beval\s+.*\$", RiskLevel.CAUTION, "Eval with variable — possible code injection"),
    (r"\bxargs\s+.*\brm\b", RiskLevel.DANGER, "xargs with rm — mass delete"),
    (r">\s*/etc/", RiskLevel.CRITICAL, "Write to /etc/"),
    (r">\s*/boot/", RiskLevel.CRITICAL, "Write to /boot/"),
]

# Suspicious file extensions (writing these is unusual for a code agent)
SUSPICIOUS_EXTENSIONS = {".exe", ".dll", ".so", ".dylib", ".bin", ".sys", ".drv", ".ko"}


# ═══════════════════════════════════════════════════════════════════════════════
# Command substitution scanner — handles nested $(...) that regex cannot
# ═══════════════════════════════════════════════════════════════════════════════

def _scan_command_substitutions(text: str) -> list[tuple[int, int, str]]:
    """Scan for $(...) substitutions with proper nesting support.

    Unlike the regex r"\\$\\([^)]+\\)" which fails on nested parens like
    $(echo $(whoami)), this scanner tracks depth and correctly extracts
    the full substitution span including nested content.

    Returns:
        List of (start_index, end_index, inner_content) tuples.
    """
    results: list[tuple[int, int, str]] = []
    i = 0
    n = len(text)
    while i < n - 1:
        if text[i:i + 2] == "$(":
            depth = 1
            j = i + 2
            while j < n and depth > 0:
                ch = text[j]
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                elif ch == "\\" and j + 1 < n:
                    j += 1  # skip escaped character
                j += 1
            if depth == 0:
                content = text[i + 2:j - 1]
                results.append((i, j, content))
                i = j
                continue
        i += 1
    return results


def _detect_ifs_bypass(text: str) -> list[str]:
    """Detect $IFS obfuscation used to evade space-delimited safety patterns.

    In bash, $IFS (or ${IFS}) expands to whitespace, so ``rm$IFS-rf$IFS/``
    is equivalent to ``rm -rf /`` but bypasses regex patterns that expect
    literal spaces between tokens.
    """
    warnings: list[str] = []
    if not re.search(r'\$IFS|\$\{IFS\}', text):
        return warnings
    # Flag any critical command combined with $IFS — the obfuscation
    # intent is clear regardless of which command it's paired with.
    dangerous = r'(?:rm|dd|mkfs|shred|chmod|chown|curl|wget|nc|bash|sh\b|zsh|fish)'
    if re.search(dangerous + r'.*\$IFS', text, re.IGNORECASE):
        match = re.search(dangerous, text, re.IGNORECASE)
        cmd = match.group(0) if match else "unknown"
        warnings.append(
            f"$IFS bypass detected: '{cmd}' with $IFS obfuscation — "
            f"shell expands $IFS to whitespace, evading token-based checks"
        )
    return warnings


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
        """Check file read safety — flag sensitive files that would leak secrets.

        Reading is allowed (the agent may legitimately need to inspect config),
        but sensitive files are flagged as CAUTION with a clear warning that
        their content will be sent to the LLM API.
        """
        # 1. Protected path check — if it's in a protected location, flag it
        protected_check = self._check_protected_path(file_path)
        if protected_check.risk.value >= RiskLevel.CAUTION.value:
            return SafetyCheck(
                allowed=True, risk=RiskLevel.CAUTION,
                warnings=[f"Sensitive file: content will be sent to LLM API. "
                          f"{protected_check.reason}"],
            )

        # 2. Sensitive filename patterns — files likely containing secrets
        basename = os.path.basename(file_path).lower()
        _SENSITIVE_NAMES = (
            ".env", ".env.", "id_rsa", "id_ed25519", "id_ecdsa",
            ".pem", ".key", ".p12", ".pfx", "credentials", "secrets",
            "authorized_keys", "known_hosts",
        )
        for sn in _SENSITIVE_NAMES:
            if sn in basename:
                return SafetyCheck(
                    allowed=True, risk=RiskLevel.CAUTION,
                    warnings=[f"Sensitive file ({basename}): content will be sent to LLM API. "
                              f"Ensure no secrets are exposed."],
                )

        # 3. Path components containing keyword hints
        path_lower = file_path.lower().replace("\\", "/")
        _SENSITIVE_DIRS = ("/.ssh/", "/.aws/", "/.azure/", "/.config/gcloud/",
                           "/.kube/", "/.gnupg/", "/etc/ssl/", "/etc/shadow",
                           "/var/run/secrets/")
        for sd in _SENSITIVE_DIRS:
            if sd in path_lower:
                return SafetyCheck(
                    allowed=True, risk=RiskLevel.CAUTION,
                    warnings=[f"Path contains sensitive directory ({sd.strip('/')}): "
                              f"content will be sent to LLM API."],
                )

        return SafetyCheck(allowed=True, risk=RiskLevel.SAFE)

    def check_write_file(self, file_path: str, content: str = "") -> SafetyCheck:
        """Check file write safety."""
        warnings = []

        # 1. Path traversal check
        traversal_check = self._check_path_traversal(file_path)
        if not traversal_check.allowed:
            self._blocked_count += 1
            return traversal_check

        # 2. Workspace boundary
        boundary_check = self._check_workspace_boundary(file_path)
        if boundary_check.risk == RiskLevel.CRITICAL:
            self._blocked_count += 1
            return boundary_check
        if boundary_check.warnings:
            warnings.extend(boundary_check.warnings)

        # 3. Protected path check
        protected_check = self._check_protected_path(file_path)
        if not protected_check.allowed:
            self._blocked_count += 1
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
            self._warned_count += 1
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
            except (OSError, UnicodeDecodeError):
                pass  # file read failed — skip old_string validation

        return write_check

    def check_shell(self, command: str) -> SafetyCheck:
        """Check shell command safety."""
        warnings = []
        highest_risk = RiskLevel.CAUTION
        critical_reasons = []

        cmd_clean = command.strip()

        # 1. Check for destructive patterns — match against the raw command
        #    so obfuscated commands (e.g. rm$IFS-rf$IFS/) are caught.
        #    We no longer strip quotes via shlex.split — that caused false
        #    positives (echo "rm -rf /" would be blocked) and false negatives
        #    (rm$IFS-rf$IFS/ would pass through).
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
            self._blocked_count += 1
            return SafetyCheck(
                allowed=False,
                risk=RiskLevel.CRITICAL,
                reason="\n".join(critical_reasons),
                warnings=warnings,
            )

        # 3. Check for pipe to shell
        if "|" in cmd_clean and any(
            s in cmd_clean.lower() for s in ("sh", "bash", "zsh", "fish")
        ):
            warnings.append("Pipe to shell detected. Verify the source.")

        # 3a. Deep scan for nested $(...) — the regex fast-path only catches
        #     simple non-nested cases; this scanner handles $(echo $(whoami)).
        subs = _scan_command_substitutions(cmd_clean)
        if subs:
            # Flag any substitution that itself contains $( — nested injection
            nested = [(s, e, c) for s, e, c in subs if "$(" in c or "`" in c]
            for start, end, content in nested:
                warnings.append(
                    f"Nested command substitution: ...{cmd_clean[max(0,start-10):end+10]}... — "
                    f"potential obfuscation"
                )
                if highest_risk < RiskLevel.DANGER:
                    highest_risk = RiskLevel.DANGER
            # Flag when command has >2 substitutions — unusual density
            if len(subs) > 2:
                warnings.append(
                    f"Multiple ({len(subs)}) command substitutions — "
                    f"unusually dense, possible injection chain"
                )
                if highest_risk < RiskLevel.CAUTION:
                    highest_risk = RiskLevel.CAUTION

        # 3b. Detect $IFS bypass attempts (rm$IFS-rf$IFS/ → rm -rf /)
        ifs_warnings = _detect_ifs_bypass(cmd_clean)
        for w in ifs_warnings:
            warnings.append(w)
            if highest_risk < RiskLevel.DANGER:
                highest_risk = RiskLevel.DANGER

        # 4. Check working directory is within workspace
        # Parse command into tokens for path extraction (separate from destructive-pattern matching)
        try:
            cmd_tokens = shlex.split(cmd_clean)
        except ValueError:
            cmd_tokens = cmd_clean.split()
        for part in cmd_tokens:
            if part.startswith("/") or part.startswith("~"):
                full = os.path.expanduser(part)
                if os.path.exists(full):
                    try:
                        Path(full).resolve().relative_to(self.workspace)
                    except ValueError:
                        warnings.append(f"Path outside workspace: {part}")
        if warnings:
            self._warned_count += 1

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

        # Only block ABSOLUTE paths that are clearly outside the workspace.
        # Relative paths are checked by _check_workspace_boundary instead.
        # Use Path.relative_to() rather than str.startswith() to avoid:
        #   C:\Foo\Bar  being considered "inside"  C:\Foo  (missing separator).
        path = Path(file_path)
        if path.is_absolute():
            try:
                resolved = path.resolve()
                resolved.relative_to(self.workspace.resolve())
            except ValueError:
                return SafetyCheck(
                    allowed=False, risk=RiskLevel.CRITICAL,
                    reason=f"Absolute path outside workspace: {file_path}",
                )
            except (OSError, RuntimeError) as e:
                logger.warning("Path resolution failed for %r: %s — blocking conservatively", file_path, e)
                return SafetyCheck(
                    allowed=False, risk=RiskLevel.DANGER,
                    reason=f"Cannot verify path is within workspace: {file_path}",
                )

        return SafetyCheck(allowed=True, risk=RiskLevel.SAFE)

    def _check_workspace_boundary(self, file_path: str) -> SafetyCheck:
        """Check if the path is within the workspace."""
        path = Path(file_path)
        if not path.is_absolute():
            path = self.workspace / path

        try:
            path.resolve().relative_to(self.workspace)
        except ValueError:
            # Path is outside workspace — block the operation
            return SafetyCheck(
                allowed=False,
                risk=RiskLevel.DANGER,
                warnings=[f"FILE OUTSIDE WORKSPACE: {file_path}\n  Workspace: {self.workspace}\n  Target: {path.resolve()}"],
            )

        return SafetyCheck(allowed=True, risk=RiskLevel.SAFE)

    def _check_protected_path(self, file_path: str) -> SafetyCheck:
        """Check if the path targets a system-protected location.

        Comparisons are case-insensitive on Windows to match the
        case-insensitive filesystem.
        """
        # Normalize the path
        normalized = os.path.normpath(file_path).replace("\\", "/")
        expanded = os.path.expanduser(normalized)

        # Try to resolve to actual path
        try:
            resolved = str(Path(expanded).resolve()).replace("\\", "/")
        except (OSError, RuntimeError):
            resolved = expanded

        # Case-insensitive on Windows
        _eq = lambda a, b: a.lower() == b.lower() if os.name == "nt" else a == b
        _sw = lambda a, b: a.lower().startswith(b.lower()) if os.name == "nt" else a.startswith(b)

        for protected in PROTECTED_PATHS:
            p = os.path.expandvars(os.path.expanduser(protected)).replace("\\", "/")
            # Strip trailing slash for exact match, keep for prefix check
            p_stripped = p.rstrip("/")
            p_resolved = p
            try:
                p_resolved = str(Path(p_stripped).resolve()).replace("\\", "/")
            except (OSError, RuntimeError):
                pass  # path resolution failed — use raw path

            # Match: expanded starts with protected path, OR is exactly the protected path
            # Using p_stripped ensures /etc matches protected path /etc/ as well
            if (_sw(expanded, p) or _eq(expanded, p_stripped)
                or _sw(resolved, p_resolved) or _eq(resolved, p_resolved)):
                return SafetyCheck(
                    allowed=False,
                    risk=RiskLevel.CRITICAL,
                    reason=f"Path is protected: {protected}",
                )

            # Suffix match for relative entries (e.g., .env, .git/config)
            # Only applies to entries that don't start with /, ~, C:\, or %
            if not (p.startswith(("/", "~", "C:", "%"))):
                # Check if the expanded path ends with "/<protected>"
                suffix = "/" + p
                expanded_lower = expanded.lower() if os.name == "nt" else expanded
                suffix_lower = suffix.lower() if os.name == "nt" else suffix
                if expanded_lower.endswith(suffix_lower):
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
