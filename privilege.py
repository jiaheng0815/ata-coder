"""
OS-aware privilege escalation system.

Handles platform-specific authorization for dangerous operations:

  Windows  → UAC elevation detection + admin check
  macOS    → osascript admin prompt + sudo biometric
  Linux    → sudo with password + root detection

Dangerous mode features:
  - Must be explicitly activated by user (/dangerous on)
  - Time-limited (auto-disable after configurable timeout)
  - Visual indicators (red UI, warnings)
  - Full audit logging of all privileged operations
  - Even in dangerous mode, critical patterns remain blocked
  - Platform-specific privilege escalation commands
"""

import base64
import logging
import os
import platform
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# OS detection
# ═══════════════════════════════════════════════════════════════════════════════

class OSFamily(Enum):
    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"
    UNKNOWN = "unknown"


def detect_os() -> OSFamily:
    system = platform.system().lower()
    if system == "windows":
        return OSFamily.WINDOWS
    if system == "darwin":
        return OSFamily.MACOS
    if system == "linux":
        return OSFamily.LINUX
    return OSFamily.UNKNOWN


def os_display() -> str:
    """Human-readable OS info."""
    return f"{platform.system()} {platform.release()} ({platform.machine()})"


# ═══════════════════════════════════════════════════════════════════════════════
# Privilege level detection
# ═══════════════════════════════════════════════════════════════════════════════

class PrivilegeLevel(Enum):
    USER = "user"        # Normal user
    ADMIN = "admin"      # Administrator / sudoer
    ROOT = "root"        # Running as root (Linux) / SYSTEM (Windows)


def detect_privilege() -> PrivilegeLevel:
    """Detect the current process's privilege level."""
    os_family = detect_os()

    if os_family == OSFamily.WINDOWS:
        try:
            import ctypes
            return PrivilegeLevel.ADMIN if ctypes.windll.shell32.IsUserAnAdmin() else PrivilegeLevel.USER
        except Exception:
            logger.debug("IsUserAnAdmin check failed, defaulting to USER")
            return PrivilegeLevel.USER

    elif os_family in (OSFamily.LINUX, OSFamily.MACOS):
        if os.geteuid() == 0:
            return PrivilegeLevel.ROOT
        # Check if user can sudo
        try:
            result = subprocess.run(
                ["sudo", "-n", "true"],
                capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                return PrivilegeLevel.ADMIN
        except Exception:
            pass
        return PrivilegeLevel.USER

    return PrivilegeLevel.USER


# ═══════════════════════════════════════════════════════════════════════════════
# Platform-specific privilege escalation
# ═══════════════════════════════════════════════════════════════════════════════

def get_elevation_prefix() -> str | None:
    """
    Get the platform-specific command prefix for privilege escalation.
    Returns None if elevation is not possible.
    """
    os_family = detect_os()
    priv = detect_privilege()

    if priv == PrivilegeLevel.ROOT:
        return None  # Already root, no prefix needed

    if os_family == OSFamily.LINUX:
        # Check if sudo is available
        try:
            subprocess.run(["which", "sudo"], capture_output=True, timeout=3, check=True)
            return "sudo"
        except Exception:
            pass
        # Try pkexec
        try:
            subprocess.run(["which", "pkexec"], capture_output=True, timeout=3, check=True)
            return "pkexec"
        except Exception:
            pass
        return None

    if os_family == OSFamily.MACOS:
        # macOS: osascript can be used for admin privileges
        return "osascript"

    if os_family == OSFamily.WINDOWS:
        # Windows: PowerShell Start-Process -Verb RunAs for elevation
        return "powershell"

    return None


def wrap_privileged_command(command: str) -> str:
    """
    Wrap a command with platform-specific privilege elevation.
    Uses shlex.quote() to prevent shell injection.

    IMPORTANT: This function assumes the *command* has already been
    validated by the safety guard. It adds an additional layer of
    quoting for the elevation wrapper but cannot sanitize an already
    malicious command.
    """
    os_family = detect_os()
    priv = detect_privilege()

    if priv == PrivilegeLevel.ROOT:
        return command

    if os_family == OSFamily.LINUX:
        # Use the detected elevation prefix (sudo or pkexec as available)
        prefix = get_elevation_prefix()
        if prefix:
            return f"{prefix} -- {shlex.quote(command)}"
        # Absolute fallback
        return f"sudo -- {shlex.quote(command)}"

    if os_family == OSFamily.MACOS:
        # osascript: double-quote the command, shlex.quote for inner safety
        return (
            "osascript -e "
            + shlex.quote(f'do shell script {shlex.quote(command)}'
                          ' with administrator privileges')
        )

    if os_family == OSFamily.WINDOWS:
        # Encode the entire command as a single base64 PowerShell script to
        # avoid nested-quoting injection through cmd.exe.  The script is
        # executed directly by PowerShell without going through cmd.exe at all,
        # which eliminates the shlex.quote / cmd.exe quoting mismatch.
        # We embed the base64 script in a here-string so that special
        # characters ($, `, ", etc.) in the original command are harmless.
        encoded = base64.b64encode(command.encode("utf-16-le")).decode()
        # Use single quotes for the inner ArgumentList so cmd.exe treats
        # them as literal characters (cmd.exe does NOT understand \" escaping).
        # PowerShell accepts single-quoted strings, which avoids cmd.exe
        # injection through the broken backslash-escape boundary.
        return (
            "powershell -Command \""
            "Start-Process -Verb RunAs -Wait -FilePath powershell.exe "
            f"-ArgumentList '-NoProfile -EncodedCommand {encoded}'"
            '"'
        )

    return command


# ═══════════════════════════════════════════════════════════════════════════════
# Dangerous mode manager
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class DangerousModeState:
    """Current state of dangerous mode."""
    enabled: bool = False
    activated_at: float = 0.0           # Unix timestamp
    timeout_minutes: int = 15            # Auto-disable after N minutes
    confirmed_by: str = ""               # How user confirmed
    level: str = "standard"              # "standard" | "elevated" | "full"
    audit_log: list[str] = field(default_factory=list)


class PrivilegeManager:
    """
    Manages dangerous mode and privilege escalation.

    Usage:
        pm = PrivilegeManager()
        pm.enable_dangerous_mode("user-typed-confirm")
        if pm.is_dangerous:
            elevated_cmd = pm.wrap_command("apt install nginx")
    """

    def __init__(self, workspace: str | Path | None = None):
        self.os_family = detect_os()
        self.privilege = detect_privilege()
        self.workspace = Path(workspace) if workspace else Path.cwd()

        self._dangerous = DangerousModeState()

        # Operations that are STILL blocked even in dangerous mode
        self._hard_blocks: list[str] = [
            "rm -rf /", "mkfs.", "dd if=/dev/zero of=/dev/",
            "> /dev/sda", "> /dev/nvme",
            "chmod 777 /", ":(){ :|:& };:",
        ]

    # ── Dangerous mode ─────────────────────────────────────────────────

    @property
    def is_dangerous(self) -> bool:
        """Check if dangerous mode is active (and not expired)."""
        if not self._dangerous.enabled:
            return False

        # Check timeout
        if self._dangerous.timeout_minutes > 0:
            elapsed = (time.monotonic() - self._dangerous.activated_at) / 60
            if elapsed > self._dangerous.timeout_minutes:
                logger.warning("Dangerous mode expired after %.1f minutes", elapsed)
                self._dangerous.enabled = False
                return False

        return True

    def enable_dangerous_mode(self, confirmed_by: str = "",
                              timeout_minutes: int = 15,
                              level: str = "standard") -> str:
        """
        Activate dangerous mode. Requires explicit confirmation.
        Returns a confirmation message.
        """
        if timeout_minutes < 0:
            raise ValueError(f"timeout_minutes must be non-negative, got {timeout_minutes}")
        if timeout_minutes == 0:
            logger.debug("Dangerous mode enabled with zero timeout (expires immediately)")
        self._dangerous.enabled = True
        self._dangerous.activated_at = time.monotonic()
        self._dangerous.timeout_minutes = timeout_minutes
        self._dangerous.confirmed_by = confirmed_by
        self._dangerous.level = level

        self._audit("DANGEROUS_MODE_ENABLED", {
            "level": level,
            "timeout": timeout_minutes,
            "os": os_display(),
            "privilege": self.privilege.value,
            "confirmed_by": confirmed_by,
        })

        msg = f"""
╔══════════════════════════════════════════════════════════╗
║  ⚠️  DANGEROUS MODE ACTIVATED                             ║
╠══════════════════════════════════════════════════════════╣
║  Level:     {level:<44}║
║  Timeout:   {timeout_minutes} minutes{'':<37}║
║  OS:        {os_display():<44}║
║  Privilege: {self.privilege.value:<44}║
╠══════════════════════════════════════════════════════════╣
║  ALL privileged operations will be AUDIT LOGGED.         ║
║  Critical system-destroying commands remain BLOCKED.     ║
║  Use /dangerous off to disable.                          ║
╚══════════════════════════════════════════════════════════╝
"""
        return msg

    def disable_dangerous_mode(self) -> str:
        """Deactivate dangerous mode."""
        was_enabled = self._dangerous.enabled
        self._dangerous = DangerousModeState()
        if was_enabled:
            self._audit("DANGEROUS_MODE_DISABLED", {})
            return "Dangerous mode disabled. Normal safety rules restored."
        return "Dangerous mode was not active."

    def status(self) -> str:
        """Get status message."""
        if self.is_dangerous:
            remaining = max(0, self._dangerous.timeout_minutes - (time.monotonic() - self._dangerous.activated_at) / 60)
            return (
                f"DANGEROUS MODE ACTIVE | "
                f"Level: {self._dangerous.level} | "
                f"OS: {self.os_family.value} | "
                f"Privilege: {self.privilege.value} | "
                f"Remaining: {remaining:.0f}min | "
                f"Audit entries: {len(self._dangerous.audit_log)}"
            )
        return (
            f"Safe mode | "
            f"OS: {self.os_family.value} | "
            f"Privilege: {self.privilege.value}"
        )

    # ── Command wrapping ──────────────────────────────────────────────

    def wrap_command(self, command: str, force_elevation: bool = False) -> str:
        """
        Wrap a command for execution, potentially with privilege escalation.
        Only elevates if dangerous mode is active AND elevation is requested.
        """
        if force_elevation and self.is_dangerous:
            return wrap_privileged_command(command)
        return command

    def check_dangerous_command(self, command: str) -> tuple[bool, str]:
        """
        Check if a command is allowed in dangerous mode.
        Returns (allowed, reason).
        """
        # Hard blocks — always denied (safety-critical, use substring match)
        cmd_clean = command.strip()
        for pattern in self._hard_blocks:
            if pattern in cmd_clean:
                return False, f"CRITICAL BLOCK (even in dangerous mode): pattern '{pattern}'"

        # In dangerous mode, most things are allowed
        if self.is_dangerous:
            return True, ""

        # Outside dangerous mode — check if command needs elevation
        needs_elev = self.needs_elevation(command)
        if needs_elev:
            return False, (
                f"This command requires elevated privileges. "
                f"Enable dangerous mode first: /dangerous on\n"
                f"  Detected OS: {os_display()}\n"
                f"  Current privilege: {self.privilege.value}\n"
                f"  Elevation command: {wrap_privileged_command(command)[:100]}..."
            )

        return True, ""

    def needs_elevation(self, command: str) -> bool:
        """Check if a command needs privilege elevation."""
        cmd_lower = command.lower().strip()

        # Package management
        package_managers = [
            "apt ", "apt-get ", "yum ", "dnf ", "pacman ", "zypper ",
            "brew ", "port ", "choco ", "winget ",
            "pip install", "pip3 install",
            "npm install -g", "npm i -g",
            "gem install",
        ]
        for pm in package_managers:
            if cmd_lower.startswith(pm):
                return True

        # System service management
        service_patterns = [
            "systemctl ", "service ", "launchctl ",
            "sc start", "sc stop", "net start", "net stop",
        ]
        for sp in service_patterns:
            if sp in cmd_lower:
                return True

        # File operations in protected areas
        # Case-insensitive check on Windows (NTFS is case-insensitive)
        _check_cmd = command.lower() if os.name == "nt" else command
        if any(p in _check_cmd for p in ["/etc/", "/usr/", "/opt/", "/var/",
                                          "c:\\program files", "c:\\windows\\system32"]):
            return True

        # Permission changes
        if any(p in cmd_lower for p in ["chmod ", "chown ", "chgrp ", "setfacl "]):
            return True

        # Network config
        if any(p in cmd_lower for p in ["ifconfig ", "ip link", "ip addr",
                                         "netsh ", "iptables ", "ufw ", "firewall-cmd"]):
            return True

        # Docker (often needs sudo — skip for read-only info commands)
        if cmd_lower.startswith("docker "):
            ro_commands = ("docker ps", "docker images", "docker info", "docker version",
                          "docker inspect", "docker logs", "docker stats", "docker system df")
            if not any(cmd_lower.startswith(c) for c in ro_commands):
                return True

        return False

    # ── Audit ───────────────────────────────────────────────────────────

    def _audit(self, event: str, details: dict) -> None:
        """Record an audited event."""
        entry = f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ')}] {event}"
        if details:
            entry += " | " + " ".join(f"{k}={v}" for k, v in details.items())
        self._dangerous.audit_log.append(entry)
        logger.info("AUDIT: %s", entry)

    def audit_operation(self, tool_name: str, arguments: dict) -> None:
        """Audit a privileged operation."""
        if self.is_dangerous:
            details = {"tool": tool_name}
            if "command" in arguments:
                details["command"] = arguments["command"][:200]
            elif "file_path" in arguments:
                details["file"] = arguments["file_path"]
            self._audit("PRIVILEGED_OP", details)

    def get_audit_log(self) -> str:
        """Get the full audit log."""
        if not self._dangerous.audit_log:
            return "(no privileged operations logged)"
        return "\n".join(self._dangerous.audit_log)

    # ── OS-specific helpers ────────────────────────────────────────────

    def get_elevation_instructions(self) -> str:
        """Get human-readable instructions for gaining privileges on this OS."""
        os_family = detect_os()
        priv = detect_privilege()

        if priv == PrivilegeLevel.ROOT:
            return "Already running as root. Full system access available."
        if priv == PrivilegeLevel.ADMIN:
            return f"Running with admin privileges on {os_display()}. Use /dangerous on to enable."

        if os_family == OSFamily.WINDOWS:
            return (
                "To gain admin privileges on Windows:\n"
                "  1. Right-click Terminal/PowerShell → Run as Administrator\n"
                "  2. Or: Start-Process -Verb RunAs python main.py\n"
                "  3. Confirm the UAC prompt"
            )
        if os_family == OSFamily.MACOS:
            return (
                "To gain admin privileges on macOS:\n"
                "  1. Prefix commands with 'sudo'\n"
                "  2. The system will prompt for your password / Touch ID\n"
                "  3. Or run the agent with: sudo python main.py"
            )
        if os_family == OSFamily.LINUX:
            return (
                "To gain admin privileges on Linux:\n"
                "  1. Prefix commands with 'sudo'\n"
                "  2. Or run the agent with: sudo python main.py\n"
                "  3. To allow passwordless sudo for specific commands, edit /etc/sudoers"
            )
        return "Unknown OS. Cannot determine elevation method."

    @property
    def can_elevate(self) -> bool:
        """Check if privilege escalation is possible on this system."""
        if self.privilege in (PrivilegeLevel.ADMIN, PrivilegeLevel.ROOT):
            return True
        return get_elevation_prefix() is not None


# ═══════════════════════════════════════════════════════════════════════════════
# Global
# ═══════════════════════════════════════════════════════════════════════════════

_privilege_manager: PrivilegeManager | None = None


def get_privilege_manager(workspace: str | None = None) -> PrivilegeManager:
    global _privilege_manager
    if _privilege_manager is None:
        _privilege_manager = PrivilegeManager(workspace)
    return _privilege_manager
