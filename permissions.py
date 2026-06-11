"""
Interactive permission system — Claude Code style.

Controls whether the agent can execute tools that modify state:
- Shell commands (run_shell)
- File writes (write_file)
- File edits (edit_file)

Permission modes (per tool type):
- ask      — prompt the user each time (default)
- allow    — always allow for this session
- deny     — always deny for this session
- once     — allow once, then revert to ask

Permissions can be configured:
- Globally via settings (permissions.json)
- Per session via interactive prompts
- Per project via .ata_coder/permissions.json

The permission prompt shows:
- The tool being called
- The arguments (truncated for readability)
- Options: [y]es, [n]o, [a]llow all, [d]eny all
"""

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


def _permissions_path() -> Path:
    """Get the permissions file path from settings or fallback."""
    try:
        from .settings import get_settings
        return get_settings().data_dir / "permissions.json"
    except Exception:
        return Path.home() / ".ata_coder" / "permissions.json"


# ── Permission mode ──────────────────────────────────────────────────────────

class PermissionMode(Enum):
    ASK = "ask"
    ALLOW = "allow"
    DENY = "deny"

    def to_label(self) -> str:
        """Human-readable label for this permission mode."""
        if self == PermissionMode.ALLOW:
            return "✅ ALLOW"
        elif self == PermissionMode.DENY:
            return "🚫 DENY"
        return "❓ ASK"


# ── Tool categories ──────────────────────────────────────────────────────────

# Tools grouped by risk level
READ_TOOLS = {"read_file", "grep", "glob", "list_dir"}
WRITE_TOOLS = {"write_file", "edit_file"}
SHELL_TOOLS = {"run_shell"}
MCP_TOOLS = set()  # populated dynamically


def tool_category(tool_name: str) -> str:
    """Get the category of a tool."""
    if tool_name in READ_TOOLS:
        return "read"
    elif tool_name in WRITE_TOOLS:
        return "write"
    elif tool_name in SHELL_TOOLS:
        return "shell"
    elif tool_name.startswith("mcp__"):
        return "mcp"
    return "other"


# ── Permission rules ─────────────────────────────────────────────────────────

@dataclass
class PermissionRule:
    """A single permission rule."""
    tool_name: str           # exact tool name or "*" for wildcard
    mode: PermissionMode
    category: str = ""        # tool category for display


class PermissionStore:
    """
    Manages permission rules and interactive prompting.

    Rules are checked in order of specificity:
    1. Exact tool name match
    2. Category match (e.g., "shell")
    3. Wildcard "*" match
    4. Default (ask)
    """

    def __init__(self, project_dir: str | Path | None = None):
        self._rules: dict[str, PermissionMode] = {}
        self._category_rules: dict[str, PermissionMode] = {}
        self._once_allowed: set[str] = set()  # tool calls allowed for one shot
        self._prompt_fn: Callable | None = None  # interactive prompt callback

        # Load project-level permissions
        self._project_dir = Path(project_dir) if project_dir else None
        self._load_project_permissions()  # always load from ~/.ata_coder/

    # ── Configuration ─────────────────────────────────────────────────────

    def set_prompt_callback(self, fn: Callable) -> None:
        """Set the function to call for interactive prompts.
        fn(tool_name, arguments, category) -> bool (allowed?)
        """
        self._prompt_fn = fn

    def set_rule(self, tool_name: str, mode: PermissionMode) -> None:
        """Set a permission rule for an exact tool name."""
        if mode == PermissionMode.ASK:
            self._rules.pop(tool_name, None)
        else:
            self._rules[tool_name] = mode

    def set_category_rule(self, category: str, mode: PermissionMode) -> None:
        """Set a permission rule for a tool category."""
        if mode == PermissionMode.ASK:
            self._category_rules.pop(category, None)
        else:
            self._category_rules[category] = mode

    def get_category_mode(self, category: str) -> PermissionMode | None:
        """Return the permission mode for a category, or None if not configured."""
        return self._category_rules.get(category)

    def allow_once(self, tool_name: str) -> None:
        """Allow a specific tool call once."""
        self._once_allowed.add(tool_name)

    # ── Permission check ──────────────────────────────────────────────────

    def check(self, tool_name: str, arguments: dict[str, Any] | None = None) -> bool:
        """
        Check if a tool call is allowed.
        Returns True if allowed, False if denied.

        For ASK mode, invokes the interactive prompt callback.
        """
        category = tool_category(tool_name)

        # 1. One-shot allow
        if tool_name in self._once_allowed:
            self._once_allowed.discard(tool_name)
            return True

        # 2. Exact tool name rule
        if tool_name in self._rules:
            mode = self._rules[tool_name]
            if mode == PermissionMode.ALLOW:
                return True
            elif mode == PermissionMode.DENY:
                logger.info("Denied by rule: %s", tool_name)
                return False

        # 3. Category rule
        if category in self._category_rules:
            mode = self._category_rules[category]
            if mode == PermissionMode.ALLOW:
                return True
            elif mode == PermissionMode.DENY:
                logger.info("Denied by category rule: %s", category)
                return False

        # 4. Wildcard rule
        if "*" in self._rules:
            mode = self._rules["*"]
            if mode == PermissionMode.ALLOW:
                return True
            elif mode == PermissionMode.DENY:
                logger.info("Denied by wildcard rule")
                return False

        # 5. Read tools always allowed (safe by default)
        if category == "read":
            return True

        # 6. Interactive prompt for write/shell/mcp
        if self._prompt_fn:
            return self._prompt_fn(tool_name, arguments or {}, category)

        # No prompt callback — deny by default for safety
        logger.warning("No prompt callback, denying: %s", tool_name)
        return False

    # ── Persistence ──────────────────────────────────────────────────────

    def _load_project_permissions(self) -> None:
        """Load permissions from ~/.ata_coder/permissions.json."""
        perms_file = _permissions_path()
        if not perms_file.exists():
            return
        try:
            with open(perms_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for tool_name, mode_str in data.get("rules", {}).items():
                try:
                    self._rules[tool_name] = PermissionMode(mode_str)
                except ValueError:
                    pass
            for category, mode_str in data.get("categories", {}).items():
                try:
                    self._category_rules[category] = PermissionMode(mode_str)
                except ValueError:
                    pass
            logger.debug("Loaded %d permission rules from project", len(data.get("rules", {})))
        except Exception as e:
            logger.warning("Failed to load project permissions: %s", e)

    def save_project_permissions(self) -> None:
        """Save permissions to ~/.ata_coder/permissions.json."""
        perms_file = _permissions_path()
        perms_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = {
                "rules": {k: v.value for k, v in self._rules.items()},
                "categories": {k: v.value for k, v in self._category_rules.items()},
            }
            with open(perms_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning("Failed to save project permissions: %s", e)

    # ── Status ──────────────────────────────────────────────────────────

    def describe(self) -> str:
        """Human-readable description of current permission state."""
        lines = ["Permission Rules:"]
        lines.append(f"  Reads: always allowed")
        for category in ["shell", "write", "mcp"]:
            if category in self._category_rules:
                lines.append(f"  {category}: {self._category_rules[category].value}")
            else:
                lines.append(f"  {category}: ask")
        for tool_name, mode in sorted(self._rules.items()):
            if tool_name != "*":
                lines.append(f"  {tool_name}: {mode.value}")
        return "\n".join(lines)


# ── Global ───────────────────────────────────────────────────────────────────

_permission_store: PermissionStore | None = None


def get_permissions(project_dir: str | None = None) -> PermissionStore:
    global _permission_store
    if _permission_store is None:
        _permission_store = PermissionStore(project_dir)
    return _permission_store
