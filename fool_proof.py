"""
Fool-proof integration layer — ties together safety guards, change tracking,
dry-run preview, and interactive confirmation.

Provides a single unified interface for "check before execute":

    check = guard.evaluate(tool_name, arguments)
    if check.needs_confirmation:
        ui.show_confirmation(check)  # interactive prompt
    if check.allowed:
        result = execute(...)
        tracker.capture(result)
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .safety_guard import SafetyGuard, SafetyCheck, RiskLevel
from .change_tracker import ChangeTracker, FileChange
from .permissions import PermissionStore, PermissionMode, tool_category

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Unified check result
# ═══════════════════════════════════════════════════════════════════════════════

class ActionRequired(Enum):
    PROCEED = "proceed"              # No confirmation needed
    CONFIRM = "confirm"              # Show confirmation prompt (y/n/a/d)
    WARN_CONFIRM = "warn_confirm"    # Show warning + confirmation
    BLOCKED = "blocked"              # Hard blocked, cannot proceed


@dataclass
class OperationCheck:
    """Complete pre-execution check result."""
    tool_name: str
    arguments: dict[str, Any]
    category: str = ""
    allowed: bool = True
    action: ActionRequired = ActionRequired.PROCEED
    risk: RiskLevel = RiskLevel.SAFE
    safety: SafetyCheck | None = None
    warnings: list[str] = field(default_factory=list)
    confirm_message: str = ""           # What to show the user
    dry_run_preview: str = ""           # What would happen in dry-run


# ═══════════════════════════════════════════════════════════════════════════════
# Fool-proof engine
# ═══════════════════════════════════════════════════════════════════════════════

class FoolProofEngine:
    """
    Unified "fool-proof" engine that combines:
    - Safety guards (pattern-based blocking)
    - Permission system (user-configurable allow/deny)
    - Change tracking (backup + undo)
    - Dry-run preview
    - Interactive confirmation

    Usage:
        engine = FoolProofEngine(workspace, permission_store, change_tracker)

        # Before executing a tool:
        check = engine.evaluate("run_shell", {"command": "rm file.txt"})
        if check.action == ActionRequired.BLOCKED:
            print(f"Blocked: {check.warnings}")
        elif check.action in (ActionRequired.CONFIRM, ActionRequired.WARN_CONFIRM):
            if ui.confirm(check):
                execute_tool()
            else:
                print("Cancelled.")

        # After executing:
        engine.capture("write_file", {"file_path": "x.py"}, result)
    """

    def __init__(self,
                 workspace: str | Path,
                 permission_store: PermissionStore | None = None,
                 change_tracker: ChangeTracker | None = None,
                 safety_guard: SafetyGuard | None = None):
        self.workspace = Path(workspace).resolve()
        self.permissions = permission_store
        self.tracker = change_tracker
        self.guard = safety_guard or SafetyGuard(workspace)

        # Stats
        self._blocks = 0
        self._confirmations = 0
        self._dry_runs = 0

    # ── Evaluate before execution ────────────────────────────────────────

    def evaluate(self, tool_name: str, arguments: dict[str, Any]) -> OperationCheck:
        """
        Evaluate a tool call BEFORE execution.
        Returns an OperationCheck indicating whether and how to proceed.
        """
        category = tool_category(tool_name)

        check = OperationCheck(
            tool_name=tool_name,
            arguments=arguments,
            category=category,
        )

        # 1. Safety guard check
        safety = self._run_safety_check(tool_name, arguments)
        check.safety = safety
        check.risk = safety.risk
        check.warnings = safety.warnings

        if not safety.allowed:
            check.allowed = False
            check.action = ActionRequired.BLOCKED
            check.confirm_message = safety.reason
            self._blocks += 1
            return check

        # 2. Read tools — always safe
        if category == "read":
            check.allowed = True
            check.action = ActionRequired.PROCEED
            return check

        # 3. For write/shell/mcp — check permissions + risk
        # NOTE: safety.risk == CRITICAL is already caught by safety.allowed above;
        # no need for a redundant check here.
        if safety.risk == RiskLevel.DANGER:
            # Check if user already allowed this category
            if self.permissions:
                cat_mode = self.permissions.get_category_mode(category)
                if cat_mode == PermissionMode.ALLOW:
                    check.action = ActionRequired.PROCEED
                elif cat_mode == PermissionMode.DENY:
                    check.action = ActionRequired.BLOCKED
                    check.allowed = False
                else:
                    check.action = ActionRequired.WARN_CONFIRM
            else:
                check.action = ActionRequired.WARN_CONFIRM

            check.confirm_message = self._format_danger_message(tool_name, arguments, safety)

        elif safety.risk == RiskLevel.CAUTION:
            if self.permissions:
                cat_mode = self.permissions.get_category_mode(category)
                if cat_mode == PermissionMode.ALLOW:
                    check.action = ActionRequired.PROCEED
                elif cat_mode == PermissionMode.DENY:
                    check.action = ActionRequired.BLOCKED
                    check.allowed = False
                else:
                    check.action = ActionRequired.CONFIRM
            else:
                check.action = ActionRequired.CONFIRM

            check.confirm_message = self._format_caution_message(tool_name, arguments, safety)

        else:
            check.action = ActionRequired.PROCEED

        # 4. Generate dry-run preview
        if self.tracker and self.tracker.dry_run:
            check.dry_run_preview = self._preview_dry_run(tool_name, arguments)

        # Track statistics
        if check.action in (ActionRequired.CONFIRM, ActionRequired.WARN_CONFIRM):
            self._confirmations += 1

        return check

    # ── Capture after execution ──────────────────────────────────────────

    def capture(self, tool_name: str, arguments: dict[str, Any],
                result: Any, old_content: str = "") -> FileChange | None:
        """Record a completed operation in the change tracker."""
        if not self.tracker:
            return None

        if tool_name == "write_file":
            file_path = arguments.get("file_path", "")
            content = arguments.get("content", "")
            if file_path:
                return self.tracker.capture_write(file_path, content)

        elif tool_name == "edit_file":
            file_path = arguments.get("file_path", "")
            if file_path and old_content:
                old_str = arguments.get("old_string", "")
                new_str = arguments.get("new_string", "")
                if old_str:
                    # Reconstruct new content from edit args (avoids re-reading from disk)
                    new_content = old_content.replace(old_str, new_str, 1)
                else:
                    # Fallback: read from disk (old_string not in args — test/legacy path)
                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            new_content = f.read()
                    except Exception:
                        new_content = old_content  # keep old content if read fails
                return self.tracker.capture_edit(file_path, old_content, new_content)

        return None

    # ── Safety check dispatch ────────────────────────────────────────────

    def _run_safety_check(self, tool_name: str, arguments: dict) -> SafetyCheck:
        if tool_name == "read_file":
            return self.guard.check_read_file(arguments.get("file_path", ""))
        if tool_name == "write_file":
            return self.guard.check_write_file(
                arguments.get("file_path", ""),
                arguments.get("content", ""),
            )
        if tool_name == "edit_file":
            return self.guard.check_edit_file(
                arguments.get("file_path", ""),
                arguments.get("old_string", ""),
                arguments.get("new_string", ""),
            )
        if tool_name == "run_shell":
            return self.guard.check_shell(arguments.get("command", ""))
        if tool_name.startswith("mcp__"):
            return SafetyCheck(allowed=True, risk=RiskLevel.CAUTION,
                             warnings=["MCP tool — verify on server side"])
        # Unknown tool — be conservative, not permissive.
        # Defaulting to SAFE would silently skip safety checks for any
        # newly-added tool that wasn't wired into this dispatch.
        return SafetyCheck(allowed=True, risk=RiskLevel.CAUTION,
                         warnings=[f"Unknown tool type: {tool_name} — no safety rules defined"])

    # ── Message formatting ───────────────────────────────────────────────

    def _format_danger_message(self, tool_name: str, arguments: dict,
                                safety: SafetyCheck) -> str:
        lines = [f"[DANGER] {tool_name}"]
        if tool_name == "run_shell" and "command" in arguments:
            lines.append(f"  Command: {arguments['command'][:200]}")
        elif "file_path" in arguments:
            lines.append(f"  File: {arguments['file_path']}")
        for w in safety.warnings:
            lines.append(f"  ! {w}")
        return "\n".join(lines)

    def _format_caution_message(self, tool_name: str, arguments: dict,
                                 safety: SafetyCheck) -> str:
        lines = [f"{tool_name}"]
        if "file_path" in arguments:
            lines.append(f"  File: {arguments['file_path']}")
        elif "command" in arguments:
            lines.append(f"  Cmd: {arguments['command'][:120]}")
        for w in safety.warnings:
            lines.append(f"  ! {w}")
        return "\n".join(lines)

    def _preview_dry_run(self, tool_name: str, arguments: dict) -> str:
        """Generate a dry-run preview string."""
        if tool_name == "write_file":
            fp = arguments.get("file_path", "")
            content = arguments.get("content", "")
            lines = content.count("\n") + 1
            return f"Would WRITE {fp} ({lines} lines, {len(content)} bytes)"
        if tool_name == "edit_file":
            fp = arguments.get("file_path", "")
            old = arguments.get("old_string", "")
            new = arguments.get("new_string", "")
            return f"Would EDIT {fp}:\n  - {old[:80]}\n  + {new[:80]}"
        if tool_name == "run_shell":
            return f"Would RUN: {arguments.get('command', '')[:200]}"
        return ""

    # ── Stats ────────────────────────────────────────────────────────────

    @property
    def stats(self) -> dict:
        return {
            "blocks": self._blocks,
            "confirmations": self._confirmations,
            "dry_runs": self._dry_runs,
            "safety_guard": self.guard.stats,
            "tracker_changes": self.tracker.count_active() if self.tracker else 0,
            "tracker_total": self.tracker.count_all() if self.tracker else 0,
        }
