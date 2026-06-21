# -*- coding: utf-8 -*-
"""Example 06: Fool-Proof System — safety, undo, dry-run with colored output."""
import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent))
import os
if sys.platform == "win32":
    try: sys.stdout.reconfigure(encoding="utf-8")
    except: pass
    os.environ.setdefault("FORCE_COLOR", "1")

import tempfile
from terminal import (ok, dim, bold, heading, critical, danger, safe, style,
                       print_separator)

def main():
    print(f"\n{heading('Fool-Proof Safety System')}")
    print_separator()

    from safety_guard import SafetyGuard, RiskLevel
    guard = SafetyGuard(os.getcwd())

    # ── Dangerous commands ────────────────────────────────────────────
    print(f"\n  {bold('Shell Command Checks:')}")
    tests = [
        ("rm -rf /", False), ("mkfs.ext4 /dev/sda", False),
        (":(){ :|:& };:", False), ("shutdown -h now", True),
        ("git push --force", True), ("ls -la", True),
    ]
    for cmd, should_allow in tests:
        c = guard.check_shell(cmd)
        icon = critical("[BLOCKED]") if not c.allowed else (danger("[DANGER]") if c.risk == RiskLevel.DANGER else ok("[OK]"))
        print(f"    {icon} {dim(cmd):<35} {style(c.risk.label, 'fail' if not c.allowed else 'warn')}")

    # ── Path protection ───────────────────────────────────────────────
    print(f"\n  {bold('Path Protection:')}")
    for p, blocked in [("/etc/passwd", True), ("~/.ssh/id_rsa", True), ("./src/main.py", False), ("../../../etc/shadow", True)]:
        c = guard.check_write_file(p)
        icon = critical("[BLOCKED]") if not c.allowed else safe("[SAFE]")
        print(f"    {icon} {dim(p):<30}")

    # ── Change tracker ────────────────────────────────────────────────
    print(f"\n  {bold('Change Tracker:')}")
    from change_tracker import ChangeTracker
    with tempfile.TemporaryDirectory() as td:
        t = ChangeTracker("demo", td)
        f = os.path.join(td, "config.py")

        t.capture_write(f, "DEBUG = True\nVERSION = '1.0'\n")
        print(f"    {dim('[1]')} CREATE  config.py  ({t.count_active()} active)")

        t.capture_edit(f, "DEBUG = True\nVERSION = '1.0'\n", "DEBUG = False\nVERSION = '2.0'\n")
        print(f"    {dim('[2]')} EDIT    config.py  ({t.count_active()} active)")

        # Show diff
        print(f"    {dim('[3]')} Diff:")
        for line in t.diff_summary(1).split("\n")[:6]:
            if line.startswith("-"): print(f"        {style(line, 'diff_del')}")
            elif line.startswith("+"): print(f"        {style(line, 'diff_add')}")
            elif line.startswith("@@"): print(f"        {style(line, 'diff_hdr')}")
            else: print(f"        {dim(line)}")

        t.undo_all()
        assert t.count_active() == 0
        print(f"    {dim('[4]')} UNDO ALL → {t.count_active()} active")

        t.restore(1)
        assert t.count_active() == 1
        print(f"    {dim('[5]')} RESTORE #1 → {t.count_active()} active")

        t.dry_run = True
        t.capture_write(os.path.join(td, "secret.py"), "SECRET = 'xxx'")
        assert not os.path.exists(os.path.join(td, "secret.py"))
        print(f"    {dim('[6]')} DRY-RUN write → file NOT created on disk")

    print(f"\n  {ok('[DONE]')} All 4 safety layers verified!")

if __name__ == "__main__":
    main()
