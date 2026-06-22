#!/usr/bin/env python3
"""
ATA Coder — Claude Code-compatible CLI.

A full-featured AI coding assistant with OpenAI-compatible APIs.

Usage:
    ata                             # Interactive mode
    ata run "Add type hints"        # Single task
    ata server                      # Start API server
    ata --skill debugger            # Interactive with forced skill
    ata --resume <session-id>       # Resume session
"""
import sys
if sys.version_info < (3, 10):
    sys.exit("Python 3.10 or higher is required for ATA Coder.")

# ── Kill GBK _readerthread errors on Windows ─────────────────────────────
# CPython's subprocess module spawns a daemon _readerthread that uses the
# system locale (GBK on Chinese Windows) when text=True.  Any non-ASCII
# output crashes the thread on interpreter shutdown with UnicodeDecodeError.
# We monkey-patch subprocess.Popen to default encoding='utf-8',errors='replace'.
if sys.platform == 'win32':
    # Windows subprocess defaults to the system ANSI code page (e.g. GBK on
    # Chinese Windows) for text-mode pipes, which corrupts UTF-8 output from
    # modern CLI tools.  This monkey-patch forces utf-8 + errors='replace' on
    # every subprocess.Popen call in the process.  It is deliberately placed
    # BEFORE any other imports so that ALL downstream Popen usage is covered.
    #
    # Risk: global side-effect that affects third-party libraries using Popen.
    # Mitigation: binary-mode calls (no encoding/text/universal_newlines) are
    # left untouched, so the patch is a no-op for the common binary-pipe case.
    # Guard 1: skip if already patched (prevents double-patch on re-import).
    # Guard 2: set ATA_CODER_NO_POPEN_PATCH=1 to disable entirely.
    import os as _os
    if _os.environ.get('ATA_CODER_NO_POPEN_PATCH', '') not in ('1', 'true', 'yes'):
        import subprocess as _sp
        if not getattr(_sp.Popen.__init__, '__ata_patched__', False):
            _orig_init = _sp.Popen.__init__
            def _patched_init(self, *a, **kw):
                if 'encoding' not in kw and 'text' not in kw and 'universal_newlines' not in kw:
                    pass  # binary mode — no encoding needed
                else:
                    kw.setdefault('encoding', 'utf-8')
                    kw.setdefault('errors', 'replace')
                _orig_init(self, *a, **kw)
            _patched_init.__ata_patched__ = True
            _sp.Popen.__init__ = _patched_init

__version__ = "1.0.1"

import asyncio
import logging
import os
import platform
import signal
import sys
from pathlib import Path

import click

# Allow running directly (python main.py) without pip install -e .
# When the package IS installed, this is a harmless no-op.
_PKG_DIR = str(Path(__file__).parent.resolve())
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)

from .config import AppConfig, get_config
from .tools import ToolExecutor, TOOL_DEFINITIONS
from .skills import get_skill_manager
from .memory import get_memory_store
from .setup_wizard import ensure_first_run as _ensure_first_run
from .session import SessionManager
from .project import ProjectDetector
from .permissions import PermissionStore, PermissionMode
from .repl_ui import ClaudeCodeUI, HAS_RICH
from .agent import CoderAgent
from .agent_subsystems import AgentSubsystems
from .clawd_integration import create_clawd_permission_handler, get_clawd

logger = logging.getLogger(__name__)

_cleanup_handlers: list = []


def register_cleanup(handler) -> None:
    _cleanup_handlers.append(handler)


def _signal_handler(sig, frame):
    # Signal handlers must be MINIMAL: no I/O, no locks, no allocations.
    # Network calls (e.g. Clawd shutdown) can deadlock if the signal
    # interrupted a thread holding a lock the network stack needs.
    # The Clawd client is shut down in agent.shutdown() during normal cleanup.
    print("\n[Interrupted]")
    for handler in _cleanup_handlers:
        try:
            handler()
        except Exception:
            pass  # cannot log from signal context
    sys.exit(1)


signal.signal(signal.SIGINT, _signal_handler)


# ── Subsystem init ──────────────────────────────────────────────────────

class SubsystemInitError(Exception):
    """Critical subsystem failed to initialize — agent cannot start."""


def _init_subsystems(config, **kwargs) -> dict:
    """Initialize all subsystems.

    Critical subsystems (skills, memory, permissions) raise on failure.
    Non-critical subsystems log a warning and continue with None.
    """
    result = {
        "skills": None, "memory": None, "mcp": None,
        "templates": None, "sessions": None, "project": None,
        "permissions": None,
    }
    workspace = config.agent.workspace_dir
    errors: list[str] = []

    def _init_subsystem(name, factory, critical=True):
        """Initialize a single subsystem with consistent error handling.

        Critical subsystems raise on failure; non-critical log a warning
        and return None.
        """
        try:
            return factory()
        except Exception as e:
            if critical:
                logger.exception("%s init failed", name)
                errors.append(f"  {name}: {e}")
            else:
                logger.warning("%s unavailable: %s", name, e)
            return None

    # ── Critical: agent cannot function without these ──────────────────
    for name, factory in [
        ("skills", lambda: get_skill_manager(kwargs.get("skills_dir"))),
        ("memory", lambda: get_memory_store(kwargs.get("memory_dir"))),
        ("permissions", lambda: PermissionStore()),
    ]:
        result[name] = _init_subsystem(name, factory, critical=True)

    # ── Non-critical: nice-to-have, degrade gracefully ─────────────────
    for name, factory in [
        ("sessions", SessionManager),
        ("templates", lambda: _try_init_templates(kwargs.get("prompts_dir"))),
        ("project", lambda: ProjectDetector(workspace).detect()),
    ]:
        result[name] = _init_subsystem(name, factory, critical=False)

    # MCP is special: only init if config provided
    result["mcp"] = _try_init_mcp(kwargs.get("mcp_config"))

    if errors:
        raise SubsystemInitError(
            "Critical subsystems failed to initialize:\n"
            + "\n".join(errors)
            + "\n\nCheck your installation or environment variables."
        )
    return result


def _try_init_templates(prompts_dir: str | None):
    from .prompt_template import TemplateManager
    return TemplateManager(prompts_dir)


def _try_init_mcp(mcp_config_path: str | None):
    """Initialize MCP client from a JSON/YAML config file path.

    Args:
        mcp_config_path: Path to an MCP configuration file (JSON or YAML).
                         If None or empty, MCP is not initialized.

    Returns:
        MCPClient instance or None.
    """
    if not mcp_config_path:
        return None
    from .mcp_client import MCPClient, load_mcp_config
    return MCPClient(load_mcp_config(mcp_config_path))


# ── Config override ─────────────────────────────────────────────────────

def _apply_config_overrides(config: AppConfig, kwargs: dict) -> str:
    explicit_model = ""
    if kwargs.get("model"):
        config.llm.model = kwargs["model"]
        explicit_model = kwargs["model"]
    if kwargs.get("api_key"):
        config.llm.api_key = kwargs["api_key"]
    if kwargs.get("base_url"):
        config.llm.base_url = kwargs["base_url"]
    if kwargs.get("workspace"):
        config.agent.workspace_dir = os.path.abspath(
            os.path.expanduser(kwargs["workspace"]))
    if kwargs.get("max_tool_calls"):
        config.agent.max_tool_calls = kwargs["max_tool_calls"]
    if kwargs.get("think"):
        config.llm.thinking_strength = kwargs["think"]
    if kwargs.get("anthropic"):
        config.llm.use_anthropic = True
    return explicit_model


# ── Startup banner / First-run setup → setup_wizard.py




# ── Interactive mode ────────────────────────────────────────────────────

async def run_interactive_async(config: AppConfig, **kwargs):
    """Async REPL loop — runs on the asyncio event loop."""
    ui = ClaudeCodeUI()

    from .commands import get_command_list
    ui.setup_command_completion(get_command_list())

    explicit_model = kwargs.get("model", "") or ""
    subsystems = _init_subsystems(config, **kwargs)

    skill_mgr = subsystems["skills"]
    memory_store = subsystems["memory"]
    mcp_client = subsystems["mcp"]
    template_mgr = subsystems["templates"]
    session_mgr = subsystems["sessions"]
    project_info = subsystems["project"]
    permission_store = subsystems["permissions"]

    def on_permission_change(action: str, target: str):
        if action == "allow_category":
            permission_store.set_category_rule(target, PermissionMode.ALLOW)
            if HAS_RICH:
                ui.console.print(f"[dim]Allowed all {target} commands for this session.[/dim]")
        elif action == "deny_category":
            permission_store.set_category_rule(target, PermissionMode.DENY)
            if HAS_RICH:
                ui.console.print(f"[dim]Denied all {target} commands for this session.[/dim]")

    ui.set_permission_callback(on_permission_change)

    # Wrap the built-in permission prompt with Clawd bubble support.
    # When Clawd is running, permission decisions go through its
    # interactive bubble UI (Y/N/A/D).  Falls back to the built-in
    # terminal prompt when Clawd is unreachable.
    _clawd_perm = create_clawd_permission_handler()
    _builtin_prompt = ui.permission_prompt

    def _combined_permission(tool_name: str, arguments: dict, category: str) -> bool:
        if _clawd_perm is not None:
            result = _clawd_perm(tool_name, arguments, category)
            if result is not None:
                return result
        return _builtin_prompt(tool_name, arguments, category)

    permission_store.set_prompt_callback(_combined_permission)

    if kwargs.get("allow_all"):
        permission_store.set_category_rule("shell", PermissionMode.ALLOW)
        permission_store.set_category_rule("write", PermissionMode.ALLOW)
    if kwargs.get("deny_shell"):
        permission_store.set_category_rule("shell", PermissionMode.DENY)

    active_skill = kwargs.get("skill") or "general-coder"
    if skill_mgr:
        skill_mgr.activate(active_skill)
    auto_skill_state = {"value": not kwargs.get("no_skill_auto", False)}

    resume_id = kwargs.get("resume")
    resume_messages = None
    if resume_id and session_mgr:
        resume_messages = session_mgr.load(resume_id)
        if resume_messages:
            if HAS_RICH:
                ui.console.print(f"[green]Resumed session: {resume_id}[/green]")
            else:
                print(f"Resumed: {resume_id}")

    mcp_names = mcp_client.connected_servers if mcp_client else []
    ui.show_welcome(
        config.llm.model, config.agent.workspace_dir,
        active_skill, project_info, mcp_names,
    )

    tool_exec = ToolExecutor(config.agent)
    tool_exec.on_edit(ui.track_edit)
    tool_exec.setup_file_cache(Path(config.agent.workspace_dir) / ".ata_coder" / "files")

    # ── Create AgentController (runs agent on background thread) ──────────
    from .agent_controller import AgentController

    subsys = AgentSubsystems(
        skills=skill_mgr, memory=memory_store, mcp=mcp_client,
        templates=template_mgr, permissions=permission_store,
        project_info=project_info, sessions=session_mgr,
    )
    controller = AgentController(
        config=config, subsystems=subsys, tool_executor=tool_exec,
    )
    await controller.start()

    # Clawd: SessionStart — one session per REPL, not per task
    _clawd = get_clawd()
    workspace_str = str(controller.agent.tools.workspace) if controller.agent else os.getcwd()
    _clawd.start(session_id=controller.agent.session_id if controller.agent else "", cwd=workspace_str)

    # Wire usage tracking (events go through EventQueue, not callback)
    if controller.agent:
        controller.agent.llm.on_usage(ui.track_usage)

    if resume_messages and controller.agent:
        controller.agent._state.messages = resume_messages
        # Sync the context manager so token tracking, compaction decisions,
        # and force-truncation all see the resumed history.
        controller.agent._context_manager.replace_all(resume_messages)
        # Reuse the resumed session ID so auto-save updates the same session
        controller.agent._current_session_id = resume_id

    running = True
    while running:
        try:
            session_info = ""
            agent_ref = controller.agent
            if agent_ref and agent_ref.session_id:
                tokens = agent_ref.get_token_estimate()
                parts = [f"tokens=~{tokens:,}"]
                if agent_ref.git:
                    gs = agent_ref.git.get_status()
                    if gs.is_dirty():
                        parts.append(f"git:{gs.summary()}")
                session_info = " ".join(parts)

            dangerous = (
                agent_ref.privilege_mgr.is_dangerous
                if agent_ref and agent_ref.privilege_mgr else False
            )
            user_input = await ui.get_input(session_info, dangerous=dangerous)
        except (KeyboardInterrupt, EOFError):
            sid = getattr(agent_ref, "_current_session_id", "") if agent_ref else ""
            if sid:
                print(f"\nResume this session with:\n  ata --resume {sid}")
            else:
                print("\nGoodbye!")
            break

        if not user_input:
            continue

        cmd, arg = _parse_command(user_input)

        if cmd is None:
            print()
            # ── Task execution via AgentController ──────────────────────
            skill_to_use = None
            if auto_skill_state["value"] and skill_mgr:
                detected = skill_mgr.detect_skill(user_input)
                if detected and detected.name != active_skill:
                    skill_to_use = detected.name
                    skill_mgr.activate(skill_to_use)
                    if HAS_RICH:
                        ui.console.print(f"  [yellow][auto-skill] {skill_to_use}[/yellow]")
                    else:
                        print(f"  [auto-skill] {skill_to_use}")

            # Clawd: notify the pet that the user submitted a new task
            get_clawd().user_prompt(prompt=user_input)

            # Submit to background thread
            await controller.submit(
                user_input, skill_name=skill_to_use,
                explicit_model=explicit_model, stream=True,
            )

            # Drain events while agent is busy (keeps UI responsive)
            try:
                while controller.is_busy():
                    for event in await controller.event_queue.drain():
                        ui.on_event(event)
                    # Check for interrupt
                    if controller._cancel.is_set():
                        break
                    await asyncio.sleep(0.05)
                # Drain remaining events after completion
                for event in await controller.event_queue.drain():
                    ui.on_event(event)
            except KeyboardInterrupt:
                await controller.cancel()
                print("\n[Interrupted]")
                ui.reset_stream()
                # Drain remaining events
                for event in await controller.event_queue.drain():
                    ui.on_event(event)
                continue
            except Exception as e:
                if HAS_RICH:
                    from rich.markup import escape as rich_escape
                    ui.console.print(f"\n[red bold]Error:[/red bold] [red]{rich_escape(str(e))}[/red]")
                else:
                    print(f"\nError: {e}")
                logger.exception("Agent run failed")
            finally:
                # Save session
                if session_mgr and controller.agent and controller.agent.session_id:
                    try:
                        controller.agent.save_session()
                    except Exception:
                        logger.warning("Failed to save session after agent run", exc_info=True)
            continue

        running = await _dispatch_command(cmd, arg, controller.agent, config, ui,
                                    skill_mgr, memory_store, session_mgr,
                                    mcp_client, template_mgr, permission_store,
                                    auto_skill_state)

    if session_mgr and controller.agent and controller.agent._state.messages:
        try:
            controller.agent.save_session()
        except Exception:
            logger.warning("Failed to save session on exit", exc_info=True)
    tool_exec.clear_file_cache()

    # Clawd: SessionEnd — await final event delivery before loop stops
    await get_clawd().shutdown_async()

    await controller.shutdown()


# ── Single task ─────────────────────────────────────────────────────────

async def run_single_task_async(task: str, config: AppConfig, **kwargs):
    ui = ClaudeCodeUI()
    subsystems = _init_subsystems(config, **kwargs)
    permission_store = subsystems["permissions"]

    # Wrap the built-in permission prompt with Clawd bubble support.
    _clawd_perm_st = create_clawd_permission_handler()
    _builtin_prompt_st = ui.permission_prompt

    def _combined_permission_st(tool_name: str, arguments: dict, category: str) -> bool:
        if _clawd_perm_st is not None:
            result = _clawd_perm_st(tool_name, arguments, category)
            if result is not None:
                return result
        return _builtin_prompt_st(tool_name, arguments, category)

    permission_store.set_prompt_callback(_combined_permission_st)

    if kwargs.get("allow_all"):
        permission_store.set_category_rule("shell", PermissionMode.ALLOW)
        permission_store.set_category_rule("write", PermissionMode.ALLOW)

    explicit_model = kwargs.get("model", "") or ""
    tool_exec = ToolExecutor(config.agent)
    tool_exec.on_edit(ui.track_edit)
    tool_exec.setup_file_cache(Path(config.agent.workspace_dir) / ".ata_coder" / "files")

    agent = CoderAgent(
        config=config, tool_executor=tool_exec,
        subsystems=AgentSubsystems(
            skills=subsystems["skills"], memory=subsystems["memory"],
            mcp=subsystems["mcp"], templates=subsystems["templates"],
            permissions=permission_store, project_info=subsystems["project"],
            sessions=subsystems["sessions"],
        ),
    )
    agent.on_event(ui.on_event)
    agent.llm.on_usage(ui.track_usage)

    skill_name = kwargs.get("skill")
    no_stream = kwargs.get("no_stream", False)

    # Clawd: for single-task mode, the session IS this task
    _clawd_st = get_clawd()
    _clawd_st.start(session_id="", cwd=str(agent.tools.workspace), title=task)
    _clawd_st.user_prompt(prompt=task)

    try:
        await agent.run(task, stream=not no_stream, skill_name=skill_name,
                  explicit_model=explicit_model)
        print()
        if subsystems["sessions"]:
            mid = agent.save_session()
            print(f"Session: {mid}")
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 1
    except Exception as e:
        print(f"\nError: {e}")
        logger.exception("Agent run failed")
        return 1
    finally:
        tool_exec.clear_file_cache()
        # Clawd: goodbye animation first
        _clawd_st.shutdown()
        await agent.shutdown()


# ── Command dispatch ────────────────────────────────────────────────────

_registry = None


async def _dispatch_command(cmd, arg, agent, config, ui, skill_mgr, memory_store,
                      session_mgr, mcp_client, template_mgr,
                      permission_store, auto_skill_state) -> bool:
    global _registry
    if _registry is None:
        from .commands import build_registry
        _registry = build_registry()

    from .commands import CommandContext
    ctx = CommandContext(
        agent=agent, config=config, ui=ui,
        skill_mgr=skill_mgr, memory_store=memory_store,
        session_mgr=session_mgr, mcp_client=mcp_client,
        template_mgr=template_mgr, permission_store=permission_store,
        auto_skill_state=auto_skill_state,
    )

    result = await _registry.dispatch(cmd, arg, ctx)
    if result is None:
        from .commands import get_command_list
        all_cmds = get_command_list()
        matches = [(n, d) for n, d in all_cmds if n.startswith(cmd)]
        if matches:
            click.echo(f"\n  Unknown: {cmd}  — Did you mean?")
            for name, desc in matches[:10]:
                click.echo(f"    {name:<18} {desc}")
        else:
            click.echo(f"\n  Unknown: {cmd}  — Available commands:")
            shown = set()
            for name, desc in sorted(all_cmds):
                if name not in shown:
                    shown.add(name)
                    click.echo(f"    {name:<18} {desc}")
        click.echo()
        return True
    return result


def _parse_command(user_input: str) -> tuple:
    if user_input.startswith("/"):
        parts = user_input.split(maxsplit=1)
        return parts[0].lower(), parts[1] if len(parts) > 1 else ""
    return None, user_input


# ═════════════════════════════════════════════════════════════════════════
# Click CLI
# ═════════════════════════════════════════════════════════════════════════

def _setup(config, kwargs):
    """Shared bootstrap: first-run check, UTF-8, logging, config, validation."""
    import io
    # Force UTF-8 on stdout/stderr — Chinese Windows defaults to GBK which
    # crashes on any Unicode character outside the GBK range (e.g. ❯ emoji).
    # The old check "not isinstance(sys.stdout, io.TextIOWrapper)" missed
    # the case where stdout IS a TextIOWrapper but with GBK encoding.
    for attr, name in [(sys, 'stdout'), (sys, 'stderr')]:
        stream = getattr(attr, name)
        if hasattr(stream, 'buffer') and stream.buffer is not None:
            needs_wrap = (
                not isinstance(stream, io.TextIOWrapper)
                or getattr(stream, 'encoding', '').lower() not in ('utf-8', 'utf8')
            )
            if needs_wrap:
                try:
                    setattr(attr, name, io.TextIOWrapper(
                        stream.buffer, encoding="utf-8", errors="replace"))
                except Exception:
                    logger.warning(
                        "Failed to wrap %s with UTF-8 encoding; "
                        "non-ASCII output may cause UnicodeDecodeError", name)

    log_level = logging.DEBUG if kwargs.get("verbose") else logging.WARNING
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from .settings import init_settings
    init_settings()

    _apply_config_overrides(config, kwargs)

    errors = config.llm.validate()
    if errors:
        click.echo("\n[!] Configuration:", err=True)
        for e in errors:
            click.echo(f"  - {e}", err=True)
        click.echo("  Run 'ata' in interactive mode to set up your API key.\n", err=True)


# Shared click options
_GLOBAL_OPTIONS = [
    click.option("--model", "-m", default=None, help="Model name"),
    click.option("--api-key", "-k", default=None, help="API key"),
    click.option("--base-url", "-b", default=None, help="API base URL"),
    click.option("--workspace", "-w", default=None, help="Workspace directory"),
    click.option("--verbose", is_flag=True, help="Verbose logging"),
    click.option("--max-tool-calls", type=int, help="Max tool calls per task"),
    click.option("--skill", "-s", default=None, help="Force a skill"),
    click.option("--skills-dir", default=None, help="Custom skills directory"),
    click.option("--no-skill-auto", is_flag=True, help="Disable skill auto-detection"),
    click.option("--memory-dir", default=None, help="Custom memory directory"),
    click.option("--mcp-config", default=None, help="MCP config JSON file"),
    click.option("--resume", "-r", default=None, help="Resume a saved session"),
    click.option("--prompts-dir", default=None, help="Custom prompts directory"),
    click.option("--allow-all", "-A", is_flag=True, help="Allow all shell/write without prompting"),
    click.option("--deny-shell", is_flag=True, help="Deny all shell commands"),
    click.option("--think", type=click.Choice(["low", "medium", "high", "xhigh", "max"]),
                 help="Enable thinking mode"),
    click.option("--anthropic", is_flag=True, help="Use Anthropic Messages API format"),
    click.option("--no-stream", "-n", is_flag=True, help="Disable streaming"),
    click.option("--version", "-v", is_flag=True, help="Show version and detailed info"),
]


def _global_options(f):
    for opt in reversed(_GLOBAL_OPTIONS):
        f = opt(f)
    return f


def _display_width(s: str) -> int:
    """Calculate the terminal display width of a string (CJK ≈ 2 cells)."""
    import unicodedata
    w = 0
    for ch in s:
        ea = unicodedata.east_asian_width(ch)
        w += 2 if ea in ("W", "F", "A") else 1  # A=Ambiguous, treated wide on CJK terminals
    return w


def _pad(s: str, target: int) -> str:
    """Pad *s* with spaces so its display width equals *target*."""
    return s + " " * max(0, target - _display_width(s))


def _print_version() -> None:
    """Print detailed version information and exit."""
    try:
        from importlib.metadata import version as pkg_version
        pkg_ver = pkg_version("ata-coder")
    except Exception:
        pkg_ver = __version__

    # Count tests dynamically
    try:
        test_dir = Path(__file__).parent / "tests"
        test_files = list(test_dir.glob("test_*.py"))
        test_count = sum(
            len([l for l in f.read_text(encoding="utf-8").splitlines()
                 if l.strip().startswith("def test_")])
            for f in test_files
        )
    except Exception:
        test_count = "?"

    # Count source files
    src_files = len(list(Path(__file__).parent.glob("*.py")))

    W = 40  # total content width inside borders

    info = [
        ("Version", pkg_ver),
        ("Python", platform.python_version()),
        ("Platform", platform.system()),
        ("Source", f"{src_files} modules, ~{test_count} tests"),
        ("Tools", f"{len(TOOL_DEFINITIONS)} built-in"),
        ("License", "MIT"),
        ("Repo", "github.com/jiaheng0815/ata-coder"),
    ]

    lines = ["┌" + "─" * (W + 2) + "┐"]
    lines.append("│  " + _pad("ATA Coder", W) + "│")
    lines.append("│" + " " * (W + 2) + "│")
    for label, value in info:
        line = f"  {label}:  {value}"
        lines.append("│" + _pad(line, W + 2) + "│")
    lines.append("└" + "─" * (W + 2) + "┘")

    click.echo("\n".join(lines))


@click.group(invoke_without_command=True)
@_global_options
@click.pass_context
def cli(ctx, **kwargs):
    """ATA Coder — AI-powered coding assistant.

    \b
    Interactive:  ata
    Single task:  ata run "your task here"
    Server:       ata server
    """
    # Version flag — print and exit before anything else
    if kwargs.get("version"):
        _print_version()
        return

    # First-run setup BEFORE config validation
    _ensure_first_run()

    config = get_config()
    _setup(config, kwargs)
    ctx.obj = {"config": config, "kwargs": kwargs}

    if ctx.invoked_subcommand is not None:
        return

    asyncio.run(run_interactive_async(config, **kwargs))


@cli.command("init")
def init_cmd():
    """Force re-run the setup wizard (overwrites existing config)."""
    _ensure_first_run(force=True)


@cli.command("_ipc")
@click.option("--workspace", default=None, help="Working directory")
@click.pass_context
def ipc_cmd(ctx, workspace):
    """Internal: JSON-RPC IPC adapter for TypeScript companion server.
    Reads JSON requests from stdin, writes JSON responses to stdout.
    Never exits unless stdin closes or 'shutdown' op is received."""
    import json as _json
    import sys as _sys
    import asyncio as _asyncio
    from pathlib import Path as _Path
    from .config import get_config as _get_config
    from .agent import CoderAgent
    from .tools.executor import ToolExecutor
    from .agent_subsystems import AgentSubsystems

    async def _ipc_loop():
        config = _get_config()
        if workspace:
            config.agent.workspace_dir = str(_Path(workspace).resolve())
        # Suppress config validation output — _ipc uses stdout for JSON-RPC,
        # and click.echo() would corrupt the protocol.
        import contextlib
        import io as _io
        _stderr_buf = _io.StringIO()
        with contextlib.redirect_stderr(_stderr_buf):
            _setup(config, {})
        tools = ToolExecutor(config.agent)
        agent = CoderAgent(config=config, tool_executor=tools,
                          subsystems=AgentSubsystems())

        for line in _sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = _json.loads(line)
            except _json.JSONDecodeError:
                continue

            op = req.get("op", "")
            rid = req.get("id", "")

            if op == "shutdown":
                await agent.shutdown()
                tools.clear_file_cache()
                _sys.stdout.write(_json.dumps({"id": rid, "status": "ok"}) + "\n")
                _sys.stdout.flush()
                break

            if op == "status":
                _sys.stdout.write(_json.dumps({"id": rid, "status": "ok"}) + "\n")
                _sys.stdout.flush()
                continue

            if op == "run":
                task = req.get("task", "")
                stream = req.get("stream", True)
                skill = req.get("skill")
                model = req.get("model", "")
                reset = req.get("resetContext", True)

                # Forward agent events as stream JSON lines when streaming.
                # The TS bridge expects {id, status:"stream", event:{type, ...}} on stdout.
                if stream:
                    from .core.events import (
                        TextDeltaEvent, ToolCallEvent, ToolResultEvent,
                        ToolStreamEvent, ThinkingEvent, ErrorEvent,
                        CompleteEvent, ReasoningEvent,
                    )

                    def _mk_stream_event(evt):
                        """Convert Python AgentEvent → TS StreamEvent dict (or None)."""
                        if isinstance(evt, TextDeltaEvent):
                            return {"type": "text_delta", "content": evt.text}
                        if isinstance(evt, ToolCallEvent):
                            return {"type": "tool_call", "tool_name": evt.tool_name,
                                    "arguments": evt.arguments}
                        if isinstance(evt, ToolResultEvent):
                            out = str(evt.result.output or "")[:4096]
                            return {"type": "tool_result", "tool_name": evt.tool_name,
                                    "success": evt.result.success, "output": out}
                        if isinstance(evt, ToolStreamEvent):
                            return {"type": "tool_stream", "tool_name": evt.tool_name,
                                    "chunk": evt.chunk}
                        if isinstance(evt, (ThinkingEvent, ReasoningEvent)):
                            content = getattr(evt, "text", "")
                            return {"type": "thinking", "content": content}
                        if isinstance(evt, ErrorEvent):
                            return {"type": "error", "message": evt.error}
                        if isinstance(evt, CompleteEvent):
                            return {"type": "complete", "text": "",
                                    "usage": {"promptTokens": 0, "completionTokens": 0,
                                              "totalTokens": evt.estimated_tokens}}
                        return None

                    def _ipc_stream_cb(evt):
                        se = _mk_stream_event(evt)
                        if se is None:
                            return
                        _sys.stdout.write(_json.dumps(
                            {"id": rid, "status": "stream", "event": se},
                            ensure_ascii=False,
                        ) + "\n")
                        _sys.stdout.flush()

                    agent.on_event(_ipc_stream_cb)

                try:
                    text = await agent.run(task, stream=stream, skill_name=skill,
                                           explicit_model=model, reset_context=reset)
                    _sys.stdout.write(_json.dumps({
                        "id": rid, "status": "done", "text": text,
                    }, ensure_ascii=False) + "\n")
                except Exception as e:
                    _sys.stdout.write(_json.dumps({
                        "id": rid, "status": "error", "error": str(e),
                    }, ensure_ascii=False) + "\n")
                finally:
                    if stream:
                        agent.on_event(None)  # unregister callback
                _sys.stdout.flush()
            else:
                _sys.stdout.write(_json.dumps({
                    "id": rid, "status": "error",
                    "error": f"Unknown op: {op}",
                }) + "\n")
                _sys.stdout.flush()

    _asyncio.run(_ipc_loop())


@cli.command("run")
@click.argument("task", required=True)
@click.pass_context
def run_cmd(ctx, task):
    """Run a single task."""
    _ensure_first_run()
    config = get_config()
    kwargs = ctx.obj.get("kwargs", {}) if ctx.obj else {}
    _setup(config, kwargs)
    ctx.exit(asyncio.run(run_single_task_async(task, config, **kwargs)))


@cli.command("server")
@click.option("--port", "-p", type=int, default=8000, help="Server port")
@click.option("--host", default="127.0.0.1", help="Server host")
@click.pass_context
def server_cmd(ctx, port, host, **kwargs):
    """Start HTTP API server."""
    _ensure_first_run()
    config = get_config()
    group_kwargs = ctx.obj.get("kwargs", {}) if ctx.obj else {}
    group_kwargs.update(kwargs)
    _setup(config, group_kwargs)

    if group_kwargs.get("allow_all"):
        os.environ["ATA_CODER_ALLOW_ALL"] = "1"

    from .model_registry import fetch_available_models
    models_list = [config.llm.model]
    click.echo(f"Fetching models from {config.llm.base_url} ...")
    fetched = fetch_available_models(config.llm.base_url, config.llm.api_key)
    if fetched:
        models_list = fetched
        click.echo(f"  {len(models_list)} model(s): {', '.join(models_list[:10])}")
    else:
        click.echo(f"  Could not fetch models, using configured: {config.llm.model}")

    os.environ["ATA_CODER_MODELS_CACHE"] = ",".join(models_list)

    from .server import create_server
    srv = create_server(config, host, port)

    click.echo(f"""
  ATA Coder API Server
  URL:     http://{host}:{port}
  Model:   {config.llm.model}
  Models:  {len(models_list)} available
  Tools:   {len(TOOL_DEFINITIONS)}
""")

    signal.signal(signal.SIGINT, signal.SIG_DFL)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    click.echo("\nServer stopped.")
    srv.shutdown()


@cli.command("gui")
@click.option("--skill", "-s", default=None, help="Force a skill")
@click.option("--port", "-p", type=int, default=0, help="Server port (0=auto)")
@click.option("--no-browser", is_flag=True, help="Don't open browser")
@click.pass_context
def gui_cmd(ctx, skill, port, no_browser, **kwargs):
    """Launch web-based GUI (opens browser)."""
    import socket
    import webbrowser
    _ensure_first_run()
    config = get_config()
    group_kwargs = ctx.obj.get("kwargs", {}) if ctx.obj else {}
    group_kwargs.update(kwargs)
    _setup(config, group_kwargs)

    overrides = ctx.obj.get("kwargs", {}) if ctx.obj else {}
    _apply_config_overrides(config, overrides)
    if skill:
        config.skill = skill

    # Show agent and server events, but suppress noisy internal modules
    logging.getLogger().setLevel(logging.WARNING)
    logging.getLogger("ata_coder.server").setLevel(logging.INFO)
    logging.getLogger("ata_coder.agent").setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("ata_coder.skills").setLevel(logging.WARNING)
    logging.getLogger("ata_coder.extension").setLevel(logging.WARNING)
    logging.getLogger("ata_coder.skill_extension").setLevel(logging.WARNING)

    # Find available port
    if port == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

    from .server import create_server
    srv = create_server(config, "127.0.0.1", port)
    url = f"http://127.0.0.1:{port}"

    click.echo(f"""
╔══════════════════════════════════════════════════╗
║         ATA Coder  —  Web GUI             ║
╠══════════════════════════════════════════════════╣
║  URL:     {url:<38}║
║  Model:   {config.llm.model:<38}║
║  Workspace: {config.agent.workspace_dir[:36]:<36}║
╚══════════════════════════════════════════════════╝
""")

    if not no_browser:
        webbrowser.open(url)

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    click.echo("Server stopped.")


def main():
    cli(standalone_mode=True)


if __name__ == "__main__":
    sys.exit(main())
