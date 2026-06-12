"""
Slash command registry — replaces the 400+ line _handle_command function.

Each command is a small self-contained function registered with a decorator.
Keeps the main loop clean and each command independently testable.
"""

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class Command:
    name: str
    handler: Callable[..., bool]  # (arg, ctx) -> continue_running
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

    def dispatch(self, cmd: str, arg: str, ctx: dict) -> bool | None:
        """Dispatch a command. Returns: True=continue, False=quit, None=unknown."""
        command = self._commands.get(cmd)
        if command is None:
            return None
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

def build_registry() -> CommandRegistry:
    r = CommandRegistry()

    # ── Basic ──────────────────────────────────────────────────────────

    @r.register("/help", "Show help", "basic")
    def cmd_help(arg: str, ctx: dict) -> bool:
        ctx["ui"].show_help()
        return True

    @r.register("/quit", "Exit", "basic")
    @r.register("/exit", "Exit", "basic")
    @r.register("/q", "Exit", "basic")
    def cmd_quit(arg: str, ctx: dict) -> bool:
        print("Goodbye!")
        return False

    @r.register("/clear", "Clear conversation", "basic")
    def cmd_clear(arg: str, ctx: dict) -> bool:
        ctx["agent"].reset()
        print("Conversation cleared.")
        return True

    @r.register("/context", "Show token usage", "basic")
    def cmd_context(arg: str, ctx: dict) -> bool:
        agent = ctx["agent"]
        tokens = agent.get_token_estimate()
        ctx["ui"].show_context(
            total_messages=len(agent._state.messages),
            tool_calls=agent._state.tool_call_count,
            skill=agent.skills.active_skill.name if agent.skills and agent.skills.active_skill else "default",
            model=ctx["config"].llm.model,
            estimated_tokens=tokens,
            max_tokens=ctx["config"].agent.max_context_tokens,
        )
        return True

    @r.register("/compact", "Compact conversation", "basic")
    def cmd_compact(arg: str, ctx: dict) -> bool:
        print(ctx["agent"].compact())
        return True

    @r.register("/cost", "Estimate cost", "basic")
    def cmd_cost(arg: str, ctx: dict) -> bool:
        from .model_registry import estimate_cost
        agent = ctx["agent"]
        tokens = agent.get_token_estimate()
        model = ctx["config"].llm.model
        cost = estimate_cost(tokens, model)
        print(f"Estimated: ~${cost:.4f} (~{tokens:,} tokens, {model})")
        return True

    @r.register("/summary", "Conversation summary", "basic")
    def cmd_summary(arg: str, ctx: dict) -> bool:
        print(ctx["agent"].get_conversation_summary())
        return True

    # ── Skills ─────────────────────────────────────────────────────────

    @r.register("/skills", "List skills", "skill")
    def cmd_skills(arg: str, ctx: dict) -> bool:
        sm = ctx.get("skill_mgr")
        if not sm:
            print("Skills not loaded.")
            return True
        for s in sm.list_skills():
            marker = " [active]" if sm.active_skill and sm.active_skill.name == s.name else ""
            print(f"  {s.name}{marker}: {s.description[:80]}")
        return True

    @r.register("/skill", "Switch skill", "skill")
    def cmd_skill(arg: str, ctx: dict) -> bool:
        sm = ctx.get("skill_mgr")
        if not sm:
            print("Skills not loaded.")
            return True
        if arg:
            s = sm.activate(arg)
            print(f"Skill: {s.name}" if s else f"Not found: {arg}")
        else:
            a = sm.active_skill
            print(f"Active: {a.name} - {a.description}" if a else "No active skill.")
        return True

    @r.register("/skill-auto", "Toggle skill auto-detect", "skill")
    def cmd_skill_auto(arg: str, ctx: dict) -> bool:
        if arg.lower() in ("off", "false", "0"):
            ctx["auto_skill_state"]["value"] = False
            print("Auto-skill: off")
        else:
            ctx["auto_skill_state"]["value"] = True
            print("Auto-skill: on")
        return True

    # ── Memory ─────────────────────────────────────────────────────────

    @r.register("/remember", "Save a memory", "memory")
    def cmd_remember(arg: str, ctx: dict) -> bool:
        store = ctx.get("memory_store")
        if not store:
            print("Memory not loaded.")
            return True
        parts = arg.split("|", 1)
        if len(parts) < 2:
            print("Usage: /remember type/name description | content")
            return True
        header = parts[0].strip()
        content = parts[1].strip()
        header_parts = header.split(maxsplit=1)
        type_name = header_parts[0]
        description = header_parts[1] if len(header_parts) > 1 else ""

        if "/" in type_name:
            mem_type, name = type_name.split("/", 1)
        else:
            mem_type, name = "reference", type_name

        from .memory import Memory
        store.add(Memory(name=name, description=description, content=content, metadata={"type": mem_type}))
        print(f"Saved: [{mem_type}] {name}")
        return True

    @r.register("/recall", "Search memories", "memory")
    def cmd_recall(arg: str, ctx: dict) -> bool:
        store = ctx.get("memory_store")
        if not store:
            print("Memory not loaded.")
            return True
        if not arg:
            print("Usage: /recall <query>")
            return True
        results = store.search(arg)
        if not results:
            print("No matches.")
            return True
        for m in results[:5]:
            print(f"\n[{m.memory_type}] {m.description}\n{m.content[:300]}")
        return True

    @r.register("/memories", "List memories", "memory")
    def cmd_memories(arg: str, ctx: dict) -> bool:
        store = ctx.get("memory_store")
        if not store:
            print("Memory not loaded.")
            return True
        memories = store.list_all(arg if arg else None)
        if not memories:
            print("No memories.")
            return True
        for m in memories:
            print(f"  [{m.memory_type}] {m.name} - {m.description} ({str(m.updated)[:10]})")
        return True

    @r.register("/forget", "Delete a memory", "memory")
    def cmd_forget(arg: str, ctx: dict) -> bool:
        store = ctx.get("memory_store")
        if not store:
            print("Memory not loaded.")
            return True
        if not arg:
            print("Usage: /forget <name>")
            return True
        ok = store.delete(arg)
        print(f"Deleted: {arg}" if ok else f"Not found: {arg}")
        return True

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

    # ── Think ──────────────────────────────────────────────────────────

    @r.register("/think", "Thinking mode", "settings")
    def cmd_think(arg: str, ctx: dict) -> bool:
        cfg = ctx["config"]
        strengths = ["off", "low", "medium", "high", "xhigh", "max"]
        if not arg:
            current = cfg.llm.thinking_strength or "off"
            print(f"Thinking: {current}  ({' | '.join(strengths)})")
        elif arg.lower() == "off":
            cfg.llm.thinking_strength = ""
            print("Thinking: OFF")
        elif arg.lower() in strengths:
            cfg.llm.thinking_strength = arg.lower()
            print(f"Thinking: {arg.upper()}")
        else:
            print(f"Invalid. Choose: {' | '.join(strengths)}")
        return True

    # ── Settings ───────────────────────────────────────────────────────

    @r.register("/model", "Change model", "settings")
    def cmd_model(arg: str, ctx: dict) -> bool:
        agent = ctx["agent"]
        if not arg:
            print(f"Model: {agent.llm.config.model}")
            return True
        agent.llm.set_model(arg)
        agent.llm.register_tools(agent._all_tools)
        print(f"Model: {arg}")
        return True

    @r.register("/effort", "Set effort: low/medium/high/max", "settings")
    def cmd_effort(arg: str, ctx: dict) -> bool:
        valid = {"low", "medium", "high", "max"}
        if not arg or arg.lower() not in valid:
            current = getattr(ctx.get("config", None), "effort", "medium")
            print(f"Effort: {current}  (low / medium / high / max)")
            print(f"  low    = fastest model, minimal tokens")
            print(f"  medium = default model, normal tokens")
            print(f"  high   = strong model, more tokens, thinking on")
            print(f"  max    = best model, max tokens, max thinking")
            return True
        level = arg.lower()
        ctx["config"].effort = level
        # Apply immediately
        agent = ctx["agent"]
        if level == "low":
            agent.llm.config.max_tokens = 4096
            agent.llm.config.thinking_strength = ""
        elif level == "medium":
            agent.llm.config.max_tokens = 16384
            agent.llm.config.thinking_strength = ""
        elif level == "high":
            agent.llm.config.max_tokens = 32768
            agent.llm.config.thinking_strength = "medium"
        elif level == "max":
            agent.llm.config.max_tokens = 65536
            agent.llm.config.thinking_strength = "high"
        print(f"Effort: {level}")
        return True

    @r.register("/models", "List models from API", "settings")
    def cmd_models(arg: str, ctx: dict) -> bool:
        from .model_registry import fetch_available_models
        cfg = ctx["config"]
        models = fetch_available_models(cfg.llm.base_url, cfg.llm.api_key)
        if not models:
            print("Failed to fetch models.")
            return True
        current = cfg.llm.model
        print(f"\n{len(models)} model(s) (current: {current}):")
        for mid in sorted(models):
            print(f"  {mid}{'  << current' if mid == current else ''}")
        return True

    @r.register("/workspace", "Change workspace", "settings")
    def cmd_workspace(arg: str, ctx: dict) -> bool:
        from pathlib import Path
        import os

        cfg = ctx["config"]
        agent = ctx["agent"]

        if not arg:
            print(f"Workspace: {cfg.agent.workspace_dir}")
            return True

        new_path = os.path.abspath(os.path.expanduser(arg))
        if not os.path.isdir(new_path):
            print(f"Not found: {arg}")
            return True

        cfg.agent.workspace_dir = new_path
        agent.tools.workspace = Path(new_path)
        agent.tools.config.workspace_dir = new_path
        print(f"Workspace: {new_path}")
        return True

    @r.register("/permissions", "Permission rules", "settings")
    def cmd_permissions(arg: str, ctx: dict) -> bool:
        ps = ctx.get("permission_store")
        print(ps.describe() if ps else "Not loaded.")
        return True

    @r.register("/mcp", "MCP status", "settings")
    def cmd_mcp(arg: str, ctx: dict) -> bool:
        mcp = ctx.get("mcp_client")
        if not mcp:
            print("MCP not configured.")
            return True
        for name in mcp.connected_servers:
            count = sum(
                1 for t in mcp.get_tools()
                if t["function"]["name"].startswith(f"mcp__{name}__")
            )
            print(f"  {name}: {count} tools")
        return True

    @r.register("/mcp-tools", "List MCP tools", "settings")
    def cmd_mcp_tools(arg: str, ctx: dict) -> bool:
        mcp = ctx.get("mcp_client")
        if not mcp:
            print("MCP not configured.")
            return True
        for t in mcp.get_tools():
            fn = t["function"]
            print(f"  {fn['name']}: {fn['description'][:100]}")
        return True

    @r.register("/templates", "List templates", "settings")
    def cmd_templates(arg: str, ctx: dict) -> bool:
        tm = ctx.get("template_mgr")
        if not tm:
            print("Not loaded.")
            return True
        for t in tm.list_templates():
            print(f"  {t}")
        return True

    @r.register("/template", "Render template", "settings")
    def cmd_template(arg: str, ctx: dict) -> bool:
        tm = ctx.get("template_mgr")
        if not tm:
            print("Not loaded.")
            return True
        if not arg:
            print("Usage: /template <name>")
            return True
        r = tm.render(arg)
        print(r if r else f"Not found: {arg}")
        return True

    # ── Sessions ───────────────────────────────────────────────────────

    @r.register("/save", "Save session", "session")
    def cmd_save(arg: str, ctx: dict) -> bool:
        print(f"Saved: {ctx['agent'].save_session(arg)}")
        return True

    @r.register("/sessions", "List all sessions", "session")
    @r.register("/history", "Search/browse history", "session")
    def cmd_history(arg: str, ctx: dict) -> bool:
        sm = ctx.get("session_mgr")
        if not sm:
            print("Session manager not available.")
            return True

        # Get current workspace for filtering
        agent = ctx["agent"]
        ws = getattr(agent.tools, "workspace", None)
        workspace = str(ws) if ws else None

        if arg:
            # Try to resume by index number
            if arg.isdigit():
                sessions = sm.list_sessions(limit=50, workspace=workspace)
                idx = int(arg) - 1
                if 0 <= idx < len(sessions):
                    meta = sessions[idx]
                    msgs = sm.load(meta.id)
                    if msgs:
                        agent._state.messages = msgs
                        print(f"Resumed: {meta.id}")
                        print(f"  {meta.summary[:100]}")
                        print(f"  Messages: {len(msgs)}, Tools: {meta.tool_call_count}")
                        return True
                print(f"No session at index {arg} (found {len(sessions)} sessions)")
                return True

            # Try to resume by session ID
            msgs = sm.load(arg)
            if msgs:
                agent._state.messages = msgs
                meta = sm.get_meta(arg)
                print(f"Resumed: {arg} ({len(msgs)} msgs)")
                if meta:
                    print(f"  {meta.summary[:100]}")
            else:
                # Search by keyword
                results = sm.search_sessions(arg, workspace=workspace)
                if results:
                    print(f"Search '{arg}': {len(results)} matches")
                    for i, meta in enumerate(results[:10], 1):
                        date = meta.created[:10] if meta.created else "?"
                        print(f"  [{i}] {date} | {meta.skill:15s} | {meta.summary[:60]}")
                else:
                    print(f"No matches for: {arg}")
            return True

        # No args — list recent sessions for this workspace
        sessions = sm.list_sessions(limit=20, workspace=workspace)
        ws_name = Path(workspace).name if workspace else "all"

        if not sessions:
            print(f"No sessions for workspace: {ws_name}")
            print("Try /history <keyword> to search all sessions.")
            return True

        print(f"History ({ws_name}/):")
        for i, meta in enumerate(sessions, 1):
            date = meta.created[:10] if meta.created else "?"
            icon = {"general-coder": "💻", "debugger": "🐛", "code-reviewer": "🔍",
                    "architect": "🏗️", "test-writer": "🧪"}.get(meta.skill, "📝")
            print(f"  [{i}] {icon} {date} | {meta.skill:15s} | {meta.summary[:60]}")
            if meta.tool_call_count:
                print(f"      {meta.message_count} msgs, {meta.tool_call_count} tools")
        print(f"\n/history <number> to resume, /history <keyword> to search")
        return True

    @r.register("/resume", "Resume session by ID", "session")
    def cmd_resume(arg: str, ctx: dict) -> bool:
        sm = ctx.get("session_mgr")
        if not sm or not arg:
            print("Usage: /resume <id>")
            return True
        msgs = sm.load(arg)
        if msgs:
            ctx["agent"]._state.messages = msgs
            print(f"Resumed: {arg} ({len(msgs)} msgs)")
        else:
            print(f"Not found: {arg}")
        return True

    @r.register("/export", "Export session", "session")
    def cmd_export(arg: str, ctx: dict) -> bool:
        sm = ctx.get("session_mgr")
        if not sm or not arg:
            print("Usage: /export <id> [path]")
            return True
        parts = arg.split(maxsplit=1)
        sid = parts[0]
        out = parts[1] if len(parts) > 1 else None
        md = sm.export_markdown(sid, out)
        if md:
            print(f"Exported {sid}" + (f" to {out}" if out else ""))
        else:
            print(f"Not found: {sid}")
        return True

    # ── Git ────────────────────────────────────────────────────────────

    @r.register("/git", "Git operations", "git")
    def cmd_git(arg: str, ctx: dict) -> bool:
        git = ctx["agent"].git
        if not git:
            print("Not available.")
            return True
        if arg == "status" or not arg:
            s = git.get_status()
            print(f"Branch: {s.branch}\nStatus: {s.summary()}")
        elif arg == "diff":
            print(git.get_diff())
        elif arg == "log":
            print(git.get_log())
        elif arg.startswith("commit"):
            ok, out = git.commit(arg[6:].strip())
            print(out)
        elif arg.startswith("branch "):
            ok, out = git.create_branch(arg[7:].strip())
            print(out)
        elif arg == "undo":
            ok, out = git.undo_commit()
            print(out)
        elif arg == "branches":
            print(git.list_branches())
        elif arg == "stash":
            git.stash()
            print("Stashed.")
        elif arg == "unstash":
            git.stash_pop()
            print("Unstashed.")
        elif arg == "summary":
            print(git.session_summary())
        else:
            print("/git [status|diff|log|commit|branch|undo|branches|stash|unstash|summary]")
        return True

    @r.register("/commit", "Git commit", "git")
    def cmd_commit(arg: str, ctx: dict) -> bool:
        git = ctx["agent"].git
        _, out = git.commit(arg) if git else (False, "Not available.")
        print(out)
        return True

    @r.register("/branch", "Git branch", "git")
    def cmd_branch(arg: str, ctx: dict) -> bool:
        git = ctx["agent"].git
        if arg:
            _, out = git.create_branch(arg)
            print(out)
        elif git:
            print(git.list_branches())
        else:
            print("Not available.")
        return True

    # ── Review & Fix ────────────────────────────────────────────────────

    @r.register("/review", "AI code review of current changes", "review")
    def cmd_review(arg: str, ctx: dict) -> bool:
        agent = ctx["agent"]
        git = agent.git
        diff_text = git.get_diff() if git else "(no git repo)"
        if not diff_text or diff_text == "(no changes)":
            print("No changes to review.")
            return True
        task = (
            "Review the following code changes. Output a structured report:\n"
            "## Issues Found\n"
            "For each: severity (critical/high/medium/low), file, line, problem, fix\n\n"
            f"```diff\n{diff_text[:8000]}\n```"
        )
        print("Reviewing changes...\n")
        agent.run(task, stream=True)
        return True

    @r.register("/fix", "AI apply review suggestions", "review")
    def cmd_fix(arg: str, ctx: dict) -> bool:
        agent = ctx["agent"]
        git = agent.git
        diff_text = git.get_diff() if git else "(no git repo)"
        if not diff_text or diff_text == "(no changes)":
            print("No changes to fix.")
            return True
        severity = arg if arg else "all"
        task = (
            f"Review this diff and fix issues. Focus on {severity} severity issues.\n"
            "Apply the fixes directly to the files.\n\n"
            f"```diff\n{diff_text[:8000]}\n```"
        )
        print(f"Fixing {severity} issues...\n")
        agent.run(task, stream=True)
        return True

    # ── Planner ────────────────────────────────────────────────────────

    @r.register("/plan", "Task plan", "plan")
    def cmd_plan(arg: str, ctx: dict) -> bool:
        p = ctx["agent"].planner
        if arg:
            agent = ctx["agent"]
            plan = p.decompose(arg, llm_client=agent.llm)
            print(plan.to_prompt())
        elif p.current_plan:
            print(p.current_plan.to_prompt())
        else:
            print("Usage: /plan <task>")
        return True

    @r.register("/tasks", "List plan tasks", "plan")
    def cmd_tasks(arg: str, ctx: dict) -> bool:
        p = ctx["agent"].planner
        if not p.current_plan:
            print("No active plan.")
            return True
        print(p.current_plan.progress_bar())
        icons = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]", "failed": "[!]", "skipped": "[-]"}
        for t in p.current_plan.subtasks:
            print(f"  {icons.get(t.status.value, '[?]')} #{t.id} {t.subject}")
        return True

    @r.register("/plan-next", "Start next task", "plan")
    def cmd_plan_next(arg: str, ctx: dict) -> bool:
        t = ctx["agent"].planner.auto_advance()
        print(f"Starting: #{t.id} {t.subject}" if t else "No pending tasks.")
        return True

    @r.register("/plan-done", "Complete task", "plan")
    def cmd_plan_done(arg: str, ctx: dict) -> bool:
        p = ctx["agent"].planner
        try: tid = int(arg) if arg else 0
        except ValueError: tid = 0
        if tid and p.current_plan:
            t = p.complete_task(tid)
        elif p.current_plan and p.current_plan.current:
            t = p.complete_task(p.current_plan.current.id)
        else:
            print("No task to complete.")
            return True
        print(f"Completed: #{t.id} {t.subject}" if t else "Failed.")
        return True

    @r.register("/retry", "Self-correct stats", "plan")
    def cmd_retry(arg: str, ctx: dict) -> bool:
        sc = ctx["agent"].self_correct
        s = sc.stats
        print(f"Retries: {s['total_retries']}  Successful: {s['successful_retries']}  "
              f"Auto-fix rate: {s['auto_fix_rate']}")
        return True

    # ── Test ──────────────────────────────────────────────────────────

    @r.register("/test", "Run project tests", "test")
    def cmd_test(arg: str, ctx: dict) -> bool:
        from .test_runner import detect_framework, run_tests
        agent = ctx["agent"]
        ws = agent.tools.workspace

        detected = detect_framework(ws)
        if not detected:
            print("No test framework detected in this project.")
            return True

        name, cmd = detected
        print(f"Framework: {name}")
        print(f"Running: {cmd}\n")

        result = run_tests(ws, cmd)
        if result is None:
            print("Test run failed to produce results.")
            return True

        print(f"\n  Passed:  {result.passed}")
        print(f"  Failed:  {result.failed}")
        print(f"  Errors:  {result.errors}")
        print(f"  Time:    {result.duration:.1f}s")
        print(f"  Status:  {'PASS' if result.ok else 'FAIL'}")

        if result.failures:
            print(f"\n  Failures:")
            for f in result.failures[:3]:
                print(f"    {f[:200]}...")
        return True

    @r.register("/test-fix", "Run tests + auto-fix failures", "test")
    def cmd_test_fix(arg: str, ctx: dict) -> bool:
        from .test_runner import auto_fix_loop
        agent = ctx["agent"]
        ws = agent.tools.workspace
        max_retries = int(arg) if arg and arg.isdigit() else 3
        passed, summary = auto_fix_loop(ws, agent, max_retries=max_retries)
        status = "PASS" if passed else "FAIL"
        print(f"\n  [{status}] {summary}")
        return True

    # ── Extensions ─────────────────────────────────────────────────────

    @r.register("/extensions", "List extensions", "extension")
    def cmd_extensions(arg: str, ctx: dict) -> bool:
        agent = ctx["agent"]
        if not hasattr(agent, "ext_mgr") or not agent.ext_mgr:
            print("Extension manager not available.")
            return True
        active_names = {e.meta.name for e in agent.ext_mgr.list_active()}
        for ext in agent.ext_mgr.list_extensions():
            status = "[active]" if ext.meta.name in active_names else "[loaded]"
            print(f"  {status} {ext.meta.name} v{ext.meta.version} — {ext.meta.description[:60]}")
        return True

    @r.register("/ext-activate", "Activate extension", "extension")
    def cmd_ext_activate(arg: str, ctx: dict) -> bool:
        if not arg:
            print("Usage: /ext-activate <name>")
            return True
        agent = ctx["agent"]
        if not hasattr(agent, "ext_mgr") or not agent.ext_mgr:
            print("Extension manager not available.")
            return True
        ok = agent.ext_mgr.activate(arg)
        print(f"Activated: {arg}" if ok else f"Failed: {arg}")
        return True

    @r.register("/ext-deactivate", "Deactivate extension", "extension")
    def cmd_ext_deactivate(arg: str, ctx: dict) -> bool:
        if not arg:
            print("Usage: /ext-deactivate <name>")
            return True
        agent = ctx["agent"]
        if not hasattr(agent, "ext_mgr") or not agent.ext_mgr:
            print("Extension manager not available.")
            return True
        ok = agent.ext_mgr.deactivate(arg)
        print(f"Deactivated: {arg}" if ok else f"Failed: {arg}")
        return True

    # ── Sub-agents ─────────────────────────────────────────────────────

    @r.register("/sub-agents", "List sub-agents", "subagent")
    def cmd_sub_agents(arg: str, ctx: dict) -> bool:
        agent = ctx["agent"]
        mgr = getattr(agent, "_sub_agent_mgr", None)
        if not mgr:
            print("SubAgentManager not available.")
            return True
        agents = mgr.list_all()
        if not agents:
            print("No sub-agents.")
            return True
        icons = {"running": "R", "done": "D", "failed": "F", "cancelled": "C", "idle": "I"}
        for a in agents:
            icon = icons.get(a.status, "?")
            print(f"  [{icon}] {a.id} — {a.status} (tool_calls={a.tool_call_count})")
            if a.status == "done" and a.result:
                print(f"       result: {a.result[:100]}...")
        return True

    @r.register("/sub-cancel", "Cancel sub-agent", "subagent")
    def cmd_sub_cancel(arg: str, ctx: dict) -> bool:
        if not arg:
            print("Usage: /sub-cancel <agent_id|all>")
            return True
        agent = ctx["agent"]
        mgr = getattr(agent, "_sub_agent_mgr", None)
        if not mgr:
            print("SubAgentManager not available.")
            return True
        if arg == "all":
            mgr.cancel_all()
            print("All sub-agents cancelled.")
        else:
            ok = mgr.cancel(arg)
            print(f"Cancelled: {arg}" if ok else f"Not found: {arg}")
        return True

    return r


# ═══════════════════════════════════════════════════════════════════════════════
# Command list for readline completion (without building full registry)
# ═══════════════════════════════════════════════════════════════════════════════

# Each entry: (name, description)
_COMMAND_LIST: list[tuple[str, str]] = [
    # Basic
    ("/help",           "Show help"),
    ("/quit",           "Exit"),
    ("/exit",           "Exit"),
    ("/q",              "Exit"),
    ("/clear",          "Clear conversation"),
    ("/context",        "Show token usage"),
    ("/compact",        "Compact conversation"),
    ("/cost",           "Estimate cost"),
    ("/summary",        "Conversation summary"),
    # Skills
    ("/skills",         "List skills"),
    ("/skill",          "Switch skill"),
    ("/skill-auto",     "Toggle skill auto-detect"),
    # Memory
    ("/remember",       "Save a memory"),
    ("/recall",         "Search memories"),
    ("/memories",       "List memories"),
    ("/forget",         "Delete a memory"),
    # Safety & Undo
    ("/undo",           "Undo changes"),
    ("/redo",           "Re-apply reverted change"),
    ("/changes",        "List file changes"),
    ("/diff-changes",   "Show change diffs"),
    ("/dry-run",        "Toggle dry-run mode"),
    ("/stats",          "Safety stats"),
    # Dangerous
    ("/dangerous",      "Dangerous mode"),
    ("/elevate",        "Elevation guide"),
    # Settings
    ("/think",          "Thinking mode"),
    ("/model",          "Change model"),
    ("/effort",         "Set effort low/medium/high/max"),
    ("/models",         "List models from API"),
    ("/workspace",      "Change workspace"),
    ("/permissions",    "Permission rules"),
    ("/mcp",            "MCP status"),
    ("/mcp-tools",      "List MCP tools"),
    ("/templates",      "List templates"),
    ("/template",       "Render template"),
    # Sessions
    ("/save",           "Save session"),
    ("/history",        "Browse/search history"),
    ("/sessions",       "List all sessions"),
    ("/resume",         "Resume session"),
    ("/export",         "Export session"),
    # Git
    ("/git",            "Git operations"),
    ("/commit",         "Git commit"),
    ("/branch",         "Git branch"),
    # Review
    ("/review",         "AI code review"),
    ("/fix",            "Apply review fixes"),
    # Plan
    ("/plan",           "Task plan"),
    ("/tasks",          "List plan tasks"),
    ("/plan-next",      "Start next task"),
    ("/plan-done",      "Complete task"),
    ("/retry",          "Self-correct stats"),
    # Test
    ("/test",           "Run project tests"),
    ("/test-fix",       "Run tests + auto-fix"),
    # Extensions
    ("/extensions",     "List extensions"),
    ("/ext-activate",   "Activate extension"),
    ("/ext-deactivate", "Deactivate extension"),
    # Sub-agents
    ("/sub-agents",     "List sub-agents"),
    ("/sub-cancel",     "Cancel sub-agent"),
]


def get_command_list() -> list[tuple[str, str]]:
    """Return list of (name, description) for all slash commands."""
    return _COMMAND_LIST
