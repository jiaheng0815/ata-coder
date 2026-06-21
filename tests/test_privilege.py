"""
Tests for privilege — OS detection, privilege levels, dangerous mode, command wrapping.
"""

import time
from unittest.mock import patch
from ata_coder.privilege import (
    OSFamily,
    detect_os,
    os_display,
    PrivilegeLevel,
    detect_privilege,
    get_elevation_prefix,
    wrap_privileged_command,
    PrivilegeManager,
    get_privilege_manager,
)


class TestOSDetection:
    """OS detection functions."""

    def test_detect_os_returns_enum(self):
        """detect_os() should return an OSFamily enum."""
        os_family = detect_os()
        assert isinstance(os_family, OSFamily)

    def test_os_display_returns_string(self):
        """os_display() should return a non-empty string."""
        display = os_display()
        assert isinstance(display, str)
        assert len(display) > 0

    def test_os_family_values(self):
        """OSFamily enum should have expected values."""
        assert OSFamily.WINDOWS.value == "windows"
        assert OSFamily.LINUX.value == "linux"
        assert OSFamily.MACOS.value == "macos"
        assert OSFamily.UNKNOWN.value == "unknown"


class TestDetectPrivilege:
    """Privilege level detection."""

    def test_detect_privilege_returns_enum(self):
        """detect_privilege() should return a PrivilegeLevel enum."""
        level = detect_privilege()
        assert isinstance(level, PrivilegeLevel)

    def test_privilege_level_values(self):
        """PrivilegeLevel enum should have expected values."""
        assert PrivilegeLevel.USER.value == "user"
        assert PrivilegeLevel.ADMIN.value == "admin"
        assert PrivilegeLevel.ROOT.value == "root"


class TestPrivilegeManagerInit:
    """PrivilegeManager initialization."""

    def test_init_detects_os(self):
        """PrivilegeManager should detect the OS family."""
        pm = PrivilegeManager()
        assert isinstance(pm.os_family, OSFamily)

    def test_init_detects_privilege(self):
        """PrivilegeManager should detect privilege level."""
        pm = PrivilegeManager()
        assert isinstance(pm.privilege, PrivilegeLevel)

    def test_dangerous_mode_disabled_by_default(self):
        """Dangerous mode should be disabled by default."""
        pm = PrivilegeManager()
        assert pm.is_dangerous is False

    def test_hard_blocks_initialized(self):
        """Hard blocks list should contain critical patterns."""
        pm = PrivilegeManager()
        assert len(pm._hard_blocks) > 0
        assert any("rm -rf /" in block for block in pm._hard_blocks)


class TestPrivilegeManagerDangerousMode:
    """Dangerous mode enable/disable and state management."""

    def test_enable_dangerous_mode(self):
        """enable_dangerous_mode should activate dangerous mode."""
        pm = PrivilegeManager()
        msg = pm.enable_dangerous_mode(confirmed_by="test_user")
        assert pm.is_dangerous is True
        assert "DANGEROUS MODE ACTIVATED" in msg

    def test_disable_dangerous_mode(self):
        """disable_dangerous_mode should deactivate."""
        pm = PrivilegeManager()
        pm.enable_dangerous_mode()
        msg = pm.disable_dangerous_mode()
        assert pm.is_dangerous is False
        assert "disabled" in msg.lower()

    def test_disable_when_not_active(self):
        """Disabling when not active should report that."""
        pm = PrivilegeManager()
        msg = pm.disable_dangerous_mode()
        assert "was not active" in msg

    def test_dangerous_mode_timeout(self):
        """Dangerous mode should auto-expire after timeout."""
        pm = PrivilegeManager()
        pm.enable_dangerous_mode(timeout_minutes=0)  # 0 = instant timeout
        # After a tiny sleep, it might still be within the same second
        # Force the timeout by setting activated_at in the past
        pm._dangerous.activated_at = time.time() - 1
        pm._dangerous.timeout_minutes = 0.01  # ~0.6 seconds
        # Actually, let's test differently:
        pm._dangerous.activated_at = 0  # epoch = 1970
        pm._dangerous.timeout_minutes = 0.001  # very short
        assert pm.is_dangerous is False

    def test_dangerous_mode_default_timeout(self):
        """Default timeout should be 15 minutes."""
        pm = PrivilegeManager()
        pm.enable_dangerous_mode()
        assert pm._dangerous.timeout_minutes == 15

    def test_dangerous_mode_custom_timeout(self):
        """Custom timeout should be respected."""
        pm = PrivilegeManager()
        pm.enable_dangerous_mode(timeout_minutes=30)
        assert pm._dangerous.timeout_minutes == 30

    def test_status_when_safe(self):
        """status() should indicate safe mode when not dangerous."""
        pm = PrivilegeManager()
        status = pm.status()
        assert "Safe mode" in status

    def test_status_when_dangerous(self):
        """status() should indicate dangerous mode when active."""
        pm = PrivilegeManager()
        pm.enable_dangerous_mode()
        status = pm.status()
        assert "DANGEROUS MODE" in status

    def test_audit_log_on_enable(self):
        """Enabling dangerous mode should create an audit entry."""
        pm = PrivilegeManager()
        pm.enable_dangerous_mode(confirmed_by="typing")
        assert len(pm._dangerous.audit_log) >= 1
        assert "DANGEROUS_MODE_ENABLED" in pm._dangerous.audit_log[0]

    def test_audit_log_on_disable(self):
        """Disabling dangerous mode should create an audit entry."""
        pm = PrivilegeManager()
        pm.enable_dangerous_mode()
        pm.disable_dangerous_mode()
        assert any("DANGEROUS_MODE_DISABLED" in entry for entry in pm._dangerous.audit_log)

    def test_get_audit_log(self):
        """get_audit_log() should return formatted entries."""
        pm = PrivilegeManager()
        log = pm.get_audit_log()
        assert log == "(no privileged operations logged)" or len(log) > 0

    def test_audit_operation(self):
        """audit_operation should log dangerous operations."""
        pm = PrivilegeManager()
        pm.enable_dangerous_mode()
        pm.audit_operation("run_shell", {"command": "sudo apt install nginx"})
        assert any("PRIVILEGED_OP" in entry for entry in pm._dangerous.audit_log)


class TestCheckDangerousCommand:
    """Command checking in dangerous mode."""

    def test_hard_block_always_denied(self):
        """Hard-blocked patterns should be denied even in dangerous mode."""
        pm = PrivilegeManager()
        pm.enable_dangerous_mode()
        allowed, reason = pm.check_dangerous_command("rm -rf /")
        assert allowed is False
        assert "CRITICAL BLOCK" in reason

    def test_normal_command_allowed_in_dangerous(self):
        """Normal commands should be allowed in dangerous mode."""
        pm = PrivilegeManager()
        pm.enable_dangerous_mode()
        allowed, _ = pm.check_dangerous_command("apt install nginx")
        assert allowed is True

    def test_command_needs_dangerous_mode(self):
        """Commands needing elevation should be denied outside dangerous mode."""
        pm = PrivilegeManager()
        # Mock the needs_elevation to return True
        with patch.object(pm, 'needs_elevation', return_value=True):
            allowed, reason = pm.check_dangerous_command("apt install nginx")
            assert allowed is False
            assert "dangerous mode" in reason.lower()

    def test_simple_command_allowed_outside_dangerous(self):
        """Simple commands should be allowed outside dangerous mode."""
        pm = PrivilegeManager()
        with patch.object(pm, 'needs_elevation', return_value=False):
            allowed, _ = pm.check_dangerous_command("ls -la")
            assert allowed is True


class TestNeedsElevation:
    """needs_elevation command detection."""

    def test_package_manager_needs_elevation(self):
        """apt commands should need elevation."""
        pm = PrivilegeManager()
        assert pm.needs_elevation("apt install nginx") is True

    def test_pip_install_needs_elevation(self):
        """pip install may need elevation."""
        pm = PrivilegeManager()
        assert pm.needs_elevation("pip install flask") is True

    def test_ls_does_not_need_elevation(self):
        """ls should not need elevation."""
        pm = PrivilegeManager()
        assert pm.needs_elevation("ls -la") is False

    def test_systemctl_needs_elevation(self):
        """systemctl commands should need elevation."""
        pm = PrivilegeManager()
        assert pm.needs_elevation("systemctl restart nginx") is True

    def test_chmod_needs_elevation(self):
        """chmod commands should need elevation."""
        pm = PrivilegeManager()
        assert pm.needs_elevation("chmod 755 script.sh") is True

    def test_chown_needs_elevation(self):
        """chown commands should need elevation."""
        pm = PrivilegeManager()
        assert pm.needs_elevation("chown root file.txt") is True

    def test_etc_path_needs_elevation(self):
        """Writing to /etc/ should need elevation."""
        pm = PrivilegeManager()
        assert pm.needs_elevation("cp file /etc/nginx/") is True

    def test_docker_command_needs_elevation(self):
        """docker commands (except ps) should need elevation."""
        pm = PrivilegeManager()
        assert pm.needs_elevation("docker run nginx") is True

    def test_docker_ps_no_elevation(self):
        """docker ps should not need elevation."""
        pm = PrivilegeManager()
        assert pm.needs_elevation("docker ps") is False


class TestWrapCommand:
    """Command wrapping for privilege escalation."""

    def test_no_wrap_without_dangerous(self):
        """Without dangerous mode, command should not be wrapped."""
        pm = PrivilegeManager()
        wrapped = pm.wrap_command("apt install nginx", force_elevation=False)
        assert wrapped == "apt install nginx"

    def test_wrap_with_force_and_dangerous(self):
        """With dangerous mode and force_elevation, should wrap."""
        pm = PrivilegeManager()
        pm.enable_dangerous_mode()
        wrapped = pm.wrap_command("apt install nginx", force_elevation=True)
        # Should be wrapped differently than bare command
        assert wrapped != "apt install nginx"

    def test_wrap_without_force(self):
        """Without force_elevation, should not wrap even in dangerous mode."""
        pm = PrivilegeManager()
        pm.enable_dangerous_mode()
        wrapped = pm.wrap_command("ls", force_elevation=False)
        assert wrapped == "ls"


class TestElevationInstructions:
    """Elevation instructions output."""

    def test_get_elevation_instructions_returns_string(self):
        """Elevation instructions should be a non-empty string."""
        pm = PrivilegeManager()
        instructions = pm.get_elevation_instructions()
        assert isinstance(instructions, str)
        assert len(instructions) > 0

    def test_can_elevate_returns_bool(self):
        """can_elevate should return a boolean."""
        pm = PrivilegeManager()
        assert isinstance(pm.can_elevate, bool)


class TestGlobalManager:
    """Global get_privilege_manager singleton."""

    def test_get_privilege_manager_singleton(self):
        """get_privilege_manager should return the same instance."""
        pm1 = get_privilege_manager()
        pm2 = get_privilege_manager()
        assert pm1 is pm2


class TestWrapPrivilegedCommand:
    """wrap_privileged_command function."""

    def test_returns_string(self):
        """wrap_privileged_command should return a string."""
        result = wrap_privileged_command("ls")
        assert isinstance(result, str)
        assert len(result) > 0


class TestGetElevationPrefix:
    """get_elevation_prefix function."""

    def test_returns_string_or_none(self):
        """get_elevation_prefix should return str or None."""
        prefix = get_elevation_prefix()
        assert prefix is None or isinstance(prefix, str)
