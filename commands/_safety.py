"""Command handlers — auto-split from commands.py."""

from __future__ import annotations
from typing import Any


def register_commands(r: Any) -> None:
    """Register this group's commands on the registry."""
    # ── Safety ────────────────────────────────────────────────────────────
# ── Safety ─────────────────────────────────────────────────────────

    @r.register("/undo", "Undo changes", "safety")
    def cmd_undo(arg: str, ctx: dict) -> bool:
        agent = ctx["agent"]
        if arg.lower() == "all":
            print(agent.undo_all())
        else:
            try:
                n = int(arg) if arg else 1
            except ValueError:
                n = 1
            print(agent.undo(n))
        return True

    @r.register("/redo", "Re-apply reverted change", "safety")
    def cmd_redo(arg: str, ctx: dict) -> bool:
        try:
            n = int(arg) if arg else 1
        except ValueError:
            print("Usage: /redo <change-id>")
            return True
        print(ctx["agent"].restore_change(n))
        return True

    @r.register("/changes", "List file changes", "safety")
    def cmd_changes(arg: str, ctx: dict) -> bool:
        print(ctx["agent"].list_changes())
        return True

    @r.register("/diff-changes", "Show change diffs", "safety")
    def cmd_diff_changes(arg: str, ctx: dict) -> bool:
        try:
            n = int(arg) if arg else 3
        except ValueError:
            n = 3
        print(ctx["agent"].show_change_diff(n))
        return True

    @r.register("/dry-run", "Toggle dry-run mode", "safety")
    def cmd_dry_run(arg: str, ctx: dict) -> bool:
        enable = None if not arg else arg.lower() in ("on", "true", "1", "yes")
        print(ctx["agent"].toggle_dry_run(enable))
        return True

    @r.register("/stats", "Safety stats", "safety")
    def cmd_stats(arg: str, ctx: dict) -> bool:
        a = ctx["agent"]
        if a.fool_proof:
            s = a.fool_proof.stats
            print(f"Blocks: {s['blocks']}  Confirmations: {s['confirmations']}  "
                  f"Changes: {s['tracker_changes']} active  "
                  f"Dry-run: {'ON' if a.change_tracker and a.change_tracker.dry_run else 'OFF'}")
        return True


    # ── Dangerous mode ────────────────────────────────────────────────────────────
# ── Dangerous mode ─────────────────────────────────────────────────

    @r.register("/dangerous", "Dangerous mode", "danger")
    def cmd_dangerous(arg: str, ctx: dict) -> bool:
        pm = ctx["agent"].privilege_mgr
        if not pm:
            print("Not available.")
            return True
        al = arg.lower()
        if al in ("on", "enable", "1", "yes"):
            print(pm.enable_dangerous_mode("user-command", timeout_minutes=15))
        elif al in ("off", "disable", "0", "no"):
            print(pm.disable_dangerous_mode())
        elif al == "audit":
            print(pm.get_audit_log())
        elif al == "elevate":
            print(pm.get_elevation_instructions())
        else:
            print(pm.status())
        return True

    @r.register("/elevate", "Elevation guide", "danger")
    def cmd_elevate(arg: str, ctx: dict) -> bool:
        pm = ctx["agent"].privilege_mgr
        print(pm.get_elevation_instructions() if pm else "Not available.")
        return True



