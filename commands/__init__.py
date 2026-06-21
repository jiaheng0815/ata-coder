"""
Slash command registry — replaces the 400+ line _handle_command function.

Each command is a small self-contained function registered with a decorator.
Command groups live in separate modules (_core, _safety, _settings, _workflow)
to keep each file focused and testable.
"""

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class CommandContext:
    """Typed context passed to every slash-command handler.

    Supports both attribute access (``ctx.agent``) and dict-style access
    (``ctx["agent"]``) for backward compatibility with existing handlers.
    """
    agent: Any = None           # CoderAgent instance
    config: Any = None          # AppConfig instance
    ui: Any = None              # ClaudeCodeUI instance
    skill_mgr: Any = None       # SkillManager
    memory_store: Any = None    # MemoryStore
    session_mgr: Any = None     # SessionManager
    mcp_client: Any = None      # MCPClient
    template_mgr: Any = None    # TemplateManager
    permission_store: Any = None  # PermissionStore
    auto_skill_state: dict = field(default_factory=lambda: {"value": True})

    def __getitem__(self, key: str) -> Any:
        """Dict-style access for backward compatibility."""
        return getattr(self, key)

    def __setitem__(self, key: str, value: Any) -> None:
        """Dict-style mutation for backward compatibility."""
        setattr(self, key, value)

    def get(self, key: str, default: Any = None) -> Any:
        """dict.get() compatibility."""
        return getattr(self, key, default)


@dataclass
class Command:
    name: str
    handler: Callable[..., bool]  # (arg: str, ctx: CommandContext) -> continue_running
    help_text: str
    category: str = "general"


class CommandRegistry:
    """Registry of slash commands with dispatch."""

    def __init__(self):
        self._commands: dict[str, Command] = {}

    def register(self, name: str, help_text: str, category: str = "general"):
        """Decorator to register a command handler."""
        def decorator(fn: Callable[..., bool]):
            self._commands[name] = Command(name=name, handler=fn, help_text=help_text, category=category)
            return fn
        return decorator

    async def dispatch(self, cmd: str, arg: str, ctx: dict) -> bool | None:
        """Dispatch a command. Returns: True=continue, False=quit, None=unknown.

        Accepts a dict for backward compatibility; wraps it in a
        CommandContext before passing to the handler.

        Supports both sync and async command handlers.
        """
        command = self._commands.get(cmd)
        if command is None:
            return None
        # Wrap dict in typed context if not already
        if isinstance(ctx, dict):
            ctx = CommandContext(**{k: ctx.get(k) for k in CommandContext.__dataclass_fields__})
        import asyncio
        if asyncio.iscoroutinefunction(command.handler):
            return await command.handler(arg, ctx)
        return command.handler(arg, ctx)

    def list_all(self) -> list[Command]:
        return sorted(self._commands.values(), key=lambda c: c.name)

    def list_by_category(self) -> dict[str, list[Command]]:
        cats: dict[str, list[Command]] = {}
        for c in self._commands.values():
            cats.setdefault(c.category, []).append(c)
        return cats


# ═══════════════════════════════════════════════════════════════════════════════
# Build the registry
# ═══════════════════════════════════════════════════════════════════════════════

def build_registry():
    """Build the command registry from all sub-modules."""
    r = CommandRegistry()

    from . import _core
    _core.register_commands(r)

    from . import _safety
    _safety.register_commands(r)

    from . import _settings
    _settings.register_commands(r)

    from . import _workflow
    _workflow.register_commands(r)

    return r

# ═══════════════════════════════════════════════════════════════════════════════
# Command list for readline completion (auto-generated from registry)
# ═══════════════════════════════════════════════════════════════════════════════

_REGISTRY: CommandRegistry | None = None


def get_command_list() -> list[tuple[str, str]]:
    """Return list of (name, description) for all slash commands."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = build_registry()
    return [(c.name, c.help_text) for c in _REGISTRY.list_all()]
