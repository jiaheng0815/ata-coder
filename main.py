#!/usr/bin/env python3
"""
ATA Coder — Claude Code-compatible CLI.

A full-featured AI coding assistant with OpenAI-compatible APIs.

Key features:
  Claude Code-level REPL with markdown, diffs, permission prompts
  Skills auto-detection (coder, reviewer, debugger, architect, tester, doc-writer)
  Persistent memory across sessions
  MCP (Model Context Protocol) support
  Session save/resume/export
  Project auto-detection
  Permission system with per-category allow/deny

Usage:
    python main.py                              # Interactive mode
    python main.py "Add type hints"             # Single task
    python main.py --skill debugger "Fix bug"   # Force skill
    python main.py --mcp-config mcp.json        # With MCP servers
    python main.py --resume <session-id>        # Resume session
"""

import argparse
import logging
import os
import signal
import sys
import time
from pathlib import Path
# Bootstrap: when run as `python main.py` (not `python -m ata_coder.main`),
# ensure the package directory is on sys.path so flat imports (e.g.
# `from .config import AppConfig`) resolve correctly.
# This is a no-op when installed via pip or run as a module.
_PKG_DIR = str(Path(__file__).parent.resolve())
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)

from .config import AppConfig, get_config
from .tools import ToolExecutor, TOOL_DEFINITIONS

# Subsystems
from .skills import get_skill_manager
from .memory import get_memory_store
from .session import SessionManager
from .project import ProjectDetector
from .permissions import PermissionStore, PermissionMode

from .repl_ui import ClaudeCodeUI, HAS_RICH
from .agent import CoderAgent
from .agent_subsystems import AgentSubsystems

logger = logging.getLogger(__name__)


# ── SIGINT → graceful exit ─────────────────────────────────────────────────────

# Registry of cleanup handlers called on SIGINT before exit.
# Subsystems register themselves so resources (HTTP clients, file handles,
# temp dirs) are properly released instead of being abandoned by os._exit().
_cleanup_handlers: list = []

def register_cleanup(handler) -> None:
    """Register a callable to run on SIGINT before the process exits."""
    _cleanup_handlers.append(handler)

def _signal_handler(sig, frame):
    print("\n[Interrupted]")
    for handler in _cleanup_handlers:
        try:
            handler()
        except Exception:
            pass
    # Use sys.exit so finally blocks, __exit__ handlers, and atexit hooks run.
    sys.exit(1)

# Install signal handler (server mode restores default)
signal.signal(signal.SIGINT, _signal_handler)


# ── Interactive mode ─────────────────────────────────────────────────────────

def run_interactive(config: AppConfig, args):
    ui = ClaudeCodeUI()

    # Enable Tab completion for slash commands
    from .commands import get_command_list
    ui.setup_command_completion(get_command_list())

    explicit_model = getattr(args, 'model', '') or ""

    # Init subsystems
    subsystems = _init_subsystems(config, args)

    skill_mgr = subsystems["skills"]
    memory_store = subsystems["memory"]
    mcp_client = subsystems["mcp"]
    template_mgr = subsystems["templates"]
    session_mgr = subsystems["sessions"]
    project_info = subsystems["project"]
    permission_store = subsystems["permissions"]

    # Wire permission store to UI
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

    # Wire permission prompt
    permission_store.set_prompt_callback(ui.permission_prompt)
    # Apply CLI permission flags
    if getattr(args, 'allow_all', False):
        permission_store.set_category_rule("shell", PermissionMode.ALLOW)
        permission_store.set_category_rule("write", PermissionMode.ALLOW)
    if getattr(args, 'deny_shell', False):
        permission_store.set_category_rule("shell", PermissionMode.DENY)

    # Initial skill
    active_skill = getattr(args, 'skill', None) or "general-coder"
    if skill_mgr:
        skill_mgr.activate(active_skill)
    auto_skill_state = {"value": getattr(args, 'skill_auto', True)}

    # Resume session
    resume_id = getattr(args, 'resume', None)
    resume_messages = None
    if resume_id and session_mgr:
        resume_messages = session_mgr.load(resume_id)
        if resume_messages:
            if HAS_RICH:
                ui.console.print(f"[green]Resumed session: {resume_id}[/green]")
            else:
                print(f"Resumed: {resume_id}")

    # Welcome
    mcp_names = mcp_client.connected_servers if mcp_client else []
    ui.show_welcome(
        config.llm.model, config.agent.workspace_dir,
        active_skill, project_info, mcp_names,
    )

    # Create tool executor with diff callback
    tool_exec = ToolExecutor(config.agent)
    tool_exec.on_edit(ui.track_edit)

    # Create agent
    agent = CoderAgent(
        config=config,
        tool_executor=tool_exec,
        subsystems=AgentSubsystems(
            skills=skill_mgr,
            memory=memory_store,
            mcp=mcp_client,
            templates=template_mgr,
            permissions=permission_store,
            project_info=project_info,
            sessions=session_mgr,
        ),
    )
    agent.on_event(ui.on_event)

    # Wire usage tracking
    agent.llm.on_usage(ui.track_usage)

    # If resuming, load messages
    if resume_messages:
        agent._state.messages = resume_messages

    running = True
    while running:
        try:
            # Show session info in prompt
            session_info = ""
            if agent.session_id:
                tokens = agent.get_token_estimate()
                parts = [f"tokens=~{tokens:,}"]
                # Git status
                if agent.git:
                    gs = agent.git.get_status()
                    if gs.is_dirty():
                        parts.append(f"git:{gs.summary()}")
                session_info = " ".join(parts)

            dangerous = agent.privilege_mgr.is_dangerous if agent.privilege_mgr else False
            user_input = ui.get_input(session_info, dangerous=dangerous)
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye!")
            break

        if not user_input:
            continue

        cmd, arg = _parse_command(user_input)

        if cmd is None:
            # ── Regular task ──────────────────────────────────────────
            print()  # visual separator before agent output
            try:
                # Auto-detect skill
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

                response = agent.run(user_input, stream=True, skill_name=skill_to_use,
                                    explicit_model=explicit_model)

                # Auto-save on completion
                if session_mgr and agent.session_id:
                    agent.save_session()

            except KeyboardInterrupt:
                print("\n[Interrupted]")
                ui.reset_stream()
                continue
            except Exception as e:
                if HAS_RICH:
                    from rich.markup import escape as rich_escape
                    ui.console.print(f"\n[red bold]Error:[/red bold] [red]{rich_escape(str(e))}[/red]")
                else:
                    print(f"\nError: {e}")
                logger.exception("Agent run failed")
            continue

        # ── Commands ──────────────────────────────────────────────────
        running = _dispatch_command(cmd, arg, agent, config, ui,
                                    skill_mgr, memory_store, session_mgr,
                                    mcp_client, template_mgr, permission_store,
                                    auto_skill_state)

    # Shutdown
    if session_mgr and agent._state.messages:
        try:
            agent.save_session()
        except Exception:
            pass
    agent.shutdown()


# ── Command handler ──────────────────────────────────────────────────────────

_registry = None  # module-level cache for command registry


def _dispatch_command(cmd: str, arg: str, agent, config, ui,
                      skill_mgr, memory_store, session_mgr,
                      mcp_client, template_mgr, permission_store,
                      auto_skill_state: dict) -> bool:
    """Dispatch slash command via registry. Returns False if should quit."""
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
        print(f"Unknown command: {cmd}  (type /help)")
        return True
    return result


# ── Single task ──────────────────────────────────────────────────────────────

def run_single_task(task: str, config: AppConfig, args):
    ui = ClaudeCodeUI()
    subsystems = _init_subsystems(config, args)
    permission_store = subsystems["permissions"]
    permission_store.set_prompt_callback(ui.permission_prompt)
    if getattr(args, 'allow_all', False):
        permission_store.set_category_rule("shell", PermissionMode.ALLOW)
        permission_store.set_category_rule("write", PermissionMode.ALLOW)

    explicit_model = getattr(args, 'model', '') or ""

    tool_exec = ToolExecutor(config.agent)
    tool_exec.on_edit(ui.track_edit)

    agent = CoderAgent(
        config=config,
        tool_executor=tool_exec,
        subsystems=AgentSubsystems(
            skills=subsystems["skills"],
            memory=subsystems["memory"],
            mcp=subsystems["mcp"],
            templates=subsystems["templates"],
            permissions=permission_store,
            project_info=subsystems["project"],
            sessions=subsystems["sessions"],
        ),
    )
    agent.on_event(ui.on_event)
    agent.llm.on_usage(ui.track_usage)

    skill_name = getattr(args, 'skill', None)
    try:
        agent.run(task, stream=not args.no_stream, skill_name=skill_name,
                 explicit_model=explicit_model)
        print()
        # Auto-save
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
        agent.shutdown()


# ── Initialization ───────────────────────────────────────────────────────────

def _init_subsystems(config, args) -> dict:
    """Initialize all subsystems and return as dict."""
    result = {
        "skills": None,
        "memory": None,
        "mcp": None,
        "templates": None,
        "sessions": None,
        "project": None,
        "permissions": None,
    }

    workspace = config.agent.workspace_dir

    # Skills
    try:
        result["skills"] = get_skill_manager(getattr(args, 'skills_dir', None))
    except Exception as e:
        logger.warning("Skills: %s", e)

    # Memory
    try:
        result["memory"] = get_memory_store(getattr(args, 'memory_dir', None))
    except Exception as e:
        logger.warning("Memory: %s", e)

    # MCP
    mcp_config = getattr(args, 'mcp_config', None)
    if mcp_config:
        try:
            from .mcp_client import MCPClient, load_mcp_config
            servers = load_mcp_config(mcp_config)
            result["mcp"] = MCPClient(servers)
        except Exception as e:
            logger.warning("MCP: %s", e)

    # Templates
    try:
        from .prompt_template import TemplateManager
        result["templates"] = TemplateManager(getattr(args, 'prompts_dir', None))
    except Exception as e:
        logger.warning("Templates: %s", e)

    # Sessions (stored in ~/.ata_coder/sessions by default)
    try:
        result["sessions"] = SessionManager()
    except Exception as e:
        logger.warning("Sessions: %s", e)

    # Project detection (scans workspace)
    try:
        detector = ProjectDetector(workspace)
        result["project"] = detector.detect()
    except Exception as e:
        logger.warning("Project detection: %s", e)

    # Permissions (stored in ~/.ata_coder/permissions.json by default)
    try:
        result["permissions"] = PermissionStore()
    except Exception as e:
        logger.warning("Permissions: %s", e)

    return result


# ── Command parsing ──────────────────────────────────────────────────────────

def _parse_command(user_input: str) -> tuple[str | None, str]:
    if user_input.startswith("/"):
        parts = user_input.split(maxsplit=1)
        return parts[0].lower(), parts[1] if len(parts) > 1 else ""
    return None, user_input


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    # Force UTF-8 output on Windows (fixes GBK encoding errors with emoji/CJK)
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="ATA Coder - Claude Code-compatible AI coding assistant",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  ata                                                 # Interactive mode
  ata "Add type hints"                                # Single task
  ata --skill debugger "Fix the bug"                  # Force skill
  ata --mcp-config mcp.example.json                   # With MCP servers
  ata --resume <session-id>                           # Resume session
  ata --allow-all                                     # Auto-allow all commands
  ata --model gpt-4o --workspace ./src                # Custom config
  ata --server --port 8000                            # Start API server
        """,
    )
    parser.add_argument("task", nargs="?", help="Task (omitted = interactive mode)")
    parser.add_argument("--model", "-m", help="Model name")
    parser.add_argument("--api-key", "-k", help="API key")
    parser.add_argument("--base-url", "-b", help="API base URL")
    parser.add_argument("--workspace", "-w", help="Workspace directory")
    parser.add_argument("--no-stream", "-n", action="store_true", help="Disable streaming")
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")
    parser.add_argument("--max-tool-calls", type=int, help="Max tool calls per task")
    # Skills
    parser.add_argument("--skill", "-s", help="Force a skill")
    parser.add_argument("--skills-dir", help="Custom skills directory")
    parser.add_argument("--no-skill-auto", action="store_true", help="Disable skill auto-detection")
    # Memory
    parser.add_argument("--memory-dir", help="Custom memory directory")
    # MCP
    parser.add_argument("--mcp-config", help="MCP config JSON file")
    # Sessions
    parser.add_argument("--resume", "-r", help="Resume a saved session")
    # Templates
    parser.add_argument("--prompts-dir", help="Custom prompts directory")
    # Permissions
    parser.add_argument("--server", action="store_true",
                        help="Start as HTTP API server")
    parser.add_argument("--port", "-p", type=int, default=8000,
                        help="Server port (for --server)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Server host (for --server)")
    parser.add_argument("--allow-all", "-A", action="store_true",
                        help="Allow all shell and write commands without prompting")
    parser.add_argument("--deny-shell", action="store_true",
                        help="Deny all shell commands")
    # Thinking mode
    parser.add_argument("--think", choices=["low", "medium", "high", "xhigh", "max"],
                        help="Enable thinking mode with strength level")
    # Anthropic API
    parser.add_argument("--anthropic", action="store_true",
                        help="Use Anthropic Messages API format (DeepSeek anthropic endpoint)")

    args = parser.parse_args()

    # Handle skill-auto
    if args.no_skill_auto:
        args.skill_auto = False
    else:
        args.skill_auto = True

    # Logging
    log_level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(level=log_level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # Config
    config = get_config()

    # Initialize ~/.ata_coder/ settings (creates dirs, seeds skills/memory)
    from .settings import init_settings
    init_settings()

    explicit_model = ""
    if args.model:
        config.llm.model = args.model
        explicit_model = args.model
    if args.api_key:
        config.llm.api_key = args.api_key
    if args.base_url:
        config.llm.base_url = args.base_url
    if args.workspace:
        config.agent.workspace_dir = os.path.abspath(os.path.expanduser(args.workspace))
    if args.max_tool_calls:
        config.agent.max_tool_calls = args.max_tool_calls
    if args.think:
        config.llm.thinking_strength = args.think
    if args.anthropic:
        os.environ["ATA_CODER_USE_ANTHROPIC"] = "1"

    # Validate
    errors = config.llm.validate()
    if errors:
        print("\n[!] Configuration:")
        for e in errors:
            print(f"  - {e}")
        print("\nSet OPENAI_API_KEY in .env or use --api-key.")
        return 1

    # Run mode: server / CLI
    if args.server:
        from .server import create_server

        if args.allow_all:
            os.environ["ATA_CODER_ALLOW_ALL"] = "1"

        # ── Fetch models from API first ──────────────────────────────
        from .model_registry import fetch_available_models
        models_list: list[str] = [config.llm.model]
        print(f"Fetching models from {config.llm.base_url} ...")
        fetched = fetch_available_models(config.llm.base_url, config.llm.api_key)
        if fetched:
            models_list = fetched
            print(f"  {len(models_list)} model(s): {', '.join(models_list[:10])}")
        else:
            print(f"  Could not fetch models, using configured: {config.llm.model}")

        # Cache models for the /models endpoint
        os.environ["ATA_CODER_MODELS_CACHE"] = ",".join(models_list)

        # ── Create and start server ──────────────────────────────────
        srv = create_server(config, args.host, args.port)

        print(f"""
  ATA Coder API Server
  URL:     http://{args.host}:{args.port}
  Model:   {config.llm.model}
  Models:  {len(models_list)} available
  Tools:   {len(TOOL_DEFINITIONS)}
""")

        # Restore default SIGINT so Ctrl+C stops the server normally
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        print("\nServer stopped.")
        srv.shutdown()
        return 0

    elif args.task:
        return run_single_task(args.task, config, args)
    else:
        run_interactive(config, args)
        return 0


if __name__ == "__main__":
    sys.exit(main())
