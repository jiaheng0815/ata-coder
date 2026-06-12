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

import logging
import os
import signal
import sys
import time
from pathlib import Path

import click

_PKG_DIR = str(Path(__file__).parent.resolve())
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)

from .config import AppConfig, get_config
from .tools import ToolExecutor, TOOL_DEFINITIONS
from .skills import get_skill_manager
from .memory import get_memory_store
from .session import SessionManager
from .project import ProjectDetector
from .permissions import PermissionStore, PermissionMode
from .repl_ui import ClaudeCodeUI, HAS_RICH
from .agent import CoderAgent
from .agent_subsystems import AgentSubsystems

logger = logging.getLogger(__name__)

_cleanup_handlers: list = []


def register_cleanup(handler) -> None:
    _cleanup_handlers.append(handler)


def _signal_handler(sig, frame):
    print("\n[Interrupted]")
    for handler in _cleanup_handlers:
        try:
            handler()
        except Exception:
            pass
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

    # ── Critical: agent cannot function without these ──────────────────
    for name, factory in [
        ("skills", lambda: get_skill_manager(kwargs.get("skills_dir"))),
        ("memory", lambda: get_memory_store(kwargs.get("memory_dir"))),
        ("permissions", lambda: PermissionStore()),
    ]:
        try:
            result[name] = factory()
        except Exception as e:
            logger.exception("%s init failed", name)
            errors.append(f"  {name}: {e}")

    # ── Non-critical: nice-to-have, degrade gracefully ─────────────────
    for name, factory in [
        ("sessions", SessionManager),
        ("templates", lambda: _try_init_templates(kwargs.get("prompts_dir"))),
        ("project", lambda: ProjectDetector(workspace).detect()),
    ]:
        try:
            result[name] = factory()
        except Exception as e:
            logger.warning("%s unavailable: %s", name, e)
            result[name] = None

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


def _try_init_mcp(mcp_config: str | None):
    if not mcp_config:
        return None
    from .mcp_client import MCPClient, load_mcp_config
    return MCPClient(load_mcp_config(mcp_config))


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
        os.environ["ATA_CODER_USE_ANTHROPIC"] = "1"
    return explicit_model


# ── First-run setup ────────────────────────────────────────────────────────

def _ensure_first_run() -> None:
    """Check ~/.ata_coder exists; if not, guide user through initial setup."""
    settings_dir = Path.home() / ".ata_coder"
    settings_file = settings_dir / "settings.json"

    # Check if already configured with a valid API key
    if settings_file.exists():
        try:
            import json as _json
            raw = settings_file.read_text(encoding="utf-8")
            data = _json.loads(raw)
            if data.get("api", {}).get("api_key", "").strip():
                return  # Already configured
        except Exception:
            pass  # Corrupt file → re-prompt

    print()
    print("=" * 56)
    print("  Welcome to ATA Coder — First Run Setup")
    print("=" * 56)
    print()
    print("  No configuration found. Let's set up your API connection.")
    print()

    # ── API Base URL ────────────────────────────────────
    default_url = "https://api.deepseek.com"
    print(f"  API Base URL [default: {default_url}]:")
    try:
        base_url = input("  > ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\n  Setup cancelled. Run 'ata' again when ready.")
        sys.exit(0)
    if not base_url:
        base_url = default_url

    # ── API Key ─────────────────────────────────────────
    print()
    print("  API Key (input will be hidden):")
    try:
        import sys as _sys
        if os.name == "nt":
            import msvcrt
            api_key_parts: list[str] = []
            while True:
                ch = msvcrt.getch()
                if ch in (b"\r", b"\n"):
                    break
                if ch == b"\x08":  # backspace
                    if api_key_parts:
                        api_key_parts.pop()
                elif ch == b"\x03":  # Ctrl+C
                    print("\n  Setup cancelled.")
                    sys.exit(0)
                else:
                    api_key_parts.append(ch.decode("utf-8", errors="replace"))
            api_key = "".join(api_key_parts)
        else:
            import tty
            import termios
            fd = _sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                api_key = ""
                while True:
                    ch = _sys.stdin.read(1)
                    if ch in ("\r", "\n"):
                        break
                    if ch == "\x03":
                        print("\n  Setup cancelled.")
                        sys.exit(0)
                    api_key += ch
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except (KeyboardInterrupt, EOFError):
        print("\n  Setup cancelled.")
        sys.exit(0)
    print()

    if not api_key.strip():
        print("  ⚠ No API key provided. You can set ATA_CODER_API_KEY later.")
        print()

    # ── Write settings ──────────────────────────────────
    settings_dir.mkdir(parents=True, exist_ok=True)
    import json as _json
    settings_data = {
        "api": {
            "base_url": base_url,
            "api_key": api_key.strip(),
        },
    }
    settings_file.write_text(
        _json.dumps(settings_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"  ✓ Settings saved to {settings_file}")
    print()


# ── Interactive mode ────────────────────────────────────────────────────

def run_interactive(config: AppConfig, **kwargs):
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
    permission_store.set_prompt_callback(ui.permission_prompt)

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
    controller.start()

    # Wire usage tracking (events go through EventQueue, not callback)
    if controller.agent:
        controller.agent.llm.on_usage(ui.track_usage)

    if resume_messages and controller.agent:
        controller.agent._state.messages = resume_messages

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
            user_input = ui.get_input(session_info, dangerous=dangerous)
        except (KeyboardInterrupt, EOFError):
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

            # Submit to background thread
            controller.submit(
                user_input, skill_name=skill_to_use,
                explicit_model=explicit_model, stream=True,
            )

            # Drain events while agent is busy (keeps UI responsive)
            try:
                while controller.is_busy():
                    for event in controller.event_queue.drain():
                        ui.on_event(event)
                    # Check for interrupt
                    if controller._cancel.is_set():
                        break
                    time.sleep(0.05)
                # Drain remaining events after completion
                for event in controller.event_queue.drain():
                    ui.on_event(event)
            except KeyboardInterrupt:
                controller.cancel()
                print("\n[Interrupted]")
                ui.reset_stream()
                # Drain remaining events
                for event in controller.event_queue.drain():
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
                        pass
            continue

        running = _dispatch_command(cmd, arg, controller.agent, config, ui,
                                    skill_mgr, memory_store, session_mgr,
                                    mcp_client, template_mgr, permission_store,
                                    auto_skill_state)

    if session_mgr and controller.agent and controller.agent._state.messages:
        try:
            controller.agent.save_session()
        except Exception:
            pass
    tool_exec.clear_file_cache()
    controller.shutdown()


# ── Single task ─────────────────────────────────────────────────────────

def run_single_task(task: str, config: AppConfig, **kwargs):
    ui = ClaudeCodeUI()
    subsystems = _init_subsystems(config, **kwargs)
    permission_store = subsystems["permissions"]
    permission_store.set_prompt_callback(ui.permission_prompt)

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
    try:
        agent.run(task, stream=not no_stream, skill_name=skill_name,
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
        agent.shutdown()


# ── Command dispatch ────────────────────────────────────────────────────

_registry = None


def _dispatch_command(cmd, arg, agent, config, ui, skill_mgr, memory_store,
                      session_mgr, mcp_client, template_mgr,
                      permission_store, auto_skill_state) -> bool:
    global _registry
    if _registry is None:
        from .commands import build_registry
        _registry = build_registry()

    ctx = {
        "agent": agent, "config": config, "ui": ui,
        "skill_mgr": skill_mgr, "memory_store": memory_store,
        "session_mgr": session_mgr, "mcp_client": mcp_client,
        "template_mgr": template_mgr, "permission_store": permission_store,
        "auto_skill_state": auto_skill_state,
    }

    result = _registry.dispatch(cmd, arg, ctx)
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
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

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
]


def _global_options(f):
    for opt in reversed(_GLOBAL_OPTIONS):
        f = opt(f)
    return f


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
    if ctx.invoked_subcommand is not None:
        return

    # First-run setup BEFORE config validation
    _ensure_first_run()

    config = get_config()
    _setup(config, kwargs)
    ctx.obj = {"config": config, "kwargs": kwargs}

    run_interactive(config, **kwargs)


@cli.command("run")
@click.argument("task", required=True)
@click.pass_context
def run_cmd(ctx, task):
    """Run a single task."""
    _ensure_first_run()
    config = get_config()
    kwargs = ctx.obj.get("kwargs", {}) if ctx.obj else {}
    _setup(config, kwargs)
    ctx.exit(run_single_task(task, config, **kwargs))


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


def main():
    cli(standalone_mode=True)


if __name__ == "__main__":
    sys.exit(main())
