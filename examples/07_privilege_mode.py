"""Example 07: Privilege & Elevation — OS-aware with colored output."""
import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent))
import os
if sys.platform == "win32":
    try: sys.stdout.reconfigure(encoding="utf-8")
    except: pass
    os.environ.setdefault("FORCE_COLOR", "1")

from terminal import ok, fail, info, dim, bold, heading, danger, critical, style, print_separator

def main():
    print(f"\n{heading('Privilege & Dangerous Mode')}")
    print_separator()

    from privilege import (detect_os, detect_privilege, OSFamily, PrivilegeLevel,
                            PrivilegeManager, wrap_privileged_command)

    os_family = detect_os()
    priv = detect_privilege()

    print(f"\n  {bold('System Info:')}")
    print(f"    OS:        {info(os_family.value)}")
    print(f"    Privilege: {style(priv.value, 'warn' if priv == PrivilegeLevel.ROOT else 'info')}")

    pm = PrivilegeManager()

    # Dangerous mode
    print(f"\n  {bold('Dangerous Mode:')}")
    print(f"    Status: {pm.status()}")
    pm.enable_dangerous_mode("demo", timeout_minutes=1)
    print(f"    {danger('[ACTIVATED]')} timeout=1min")

    # Elevation test
    original = "apt install nginx"
    elevated = pm.wrap_command(original, force_elevation=True)
    print(f"\n  {bold('Command Wrapping:')}")
    print(f"    {dim('Original:')}  {original}")
    print(f"    {info('Elevated:')}  {elevated[:100]}...")

    # Hard blocks
    print(f"\n  {bold('Hard Blocks (even in dangerous mode):')}")
    for cmd in ["rm -rf /", "mkfs.ext4 /dev/sda"]:
        allowed, reason = pm.check_dangerous_command(cmd)
        print(f"    {critical('[BLOCKED]') if not allowed else ok('[OK]')} {cmd}")

    # Audit
    pm.audit_operation("run_shell", {"command": "systemctl restart nginx"})
    pm.audit_operation("write_file", {"file_path": "/etc/nginx/nginx.conf"})
    log = pm.get_audit_log()
    print(f"\n  {bold('Audit Log:')} {dim(f'{len(pm._dangerous.audit_log)} entries')}")

    pm.disable_dangerous_mode()
    print(f"\n  {bold('Instructions:')}")
    for line in pm.get_elevation_instructions().split("\n")[:5]:
        print(f"    {dim(line)}")

    print(f"\n  {ok('[DONE]')} Cross-platform privilege system ready!")

if __name__ == "__main__":
    main()
