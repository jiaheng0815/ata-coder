"""Command handlers — auto-split from commands.py."""

from __future__ import annotations
from pathlib import Path
from typing import Any


def register_commands(r: Any) -> None:
    """Register this group's commands on the registry."""
    # ── Sessions ────────────────────────────────────────────────────────────
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
        print("\n/history <number> to resume, /history <keyword> to search")
        return True

    @r.register("/resume", "Resume or list sessions", "session")
    def cmd_resume(arg: str, ctx: dict) -> bool:
        """
        /resume        → list sessions in current workspace
        /resume <hash> → resume a specific session by ID or hash
        """
        sm = ctx.get("session_mgr")
        if not sm:
            print("Session storage not available.")
            return True

        # ── No arg: list sessions in current workspace ───────────────
        if not arg:
            import hashlib
            try:
                workspace = str(ctx["agent"].tools.workspace) if ctx.get("agent") else ""
            except Exception:
                workspace = ""
            ws_hash = hashlib.sha256(workspace.encode()).hexdigest()[:8]
            sessions = sm.list_sessions(limit=50)
            matches = [s for s in sessions if s.id.startswith(ws_hash)]

            if not matches:
                print(f"No sessions in this workspace.\n  hash: {ws_hash}")
                return True

            print(f"\nSessions ({workspace}):\n")
            for i, s in enumerate(matches):
                parts = s.id.split("-")
                resume_hash = parts[-1] if len(parts) >= 3 else s.id[-8:]
                title = s.summary[:60] if s.summary else "(untitled)"
                print(f"  [{i+1}] {title}")
                print(f"      ata --resume {resume_hash}")
                print(f"      {s.updated}  {s.message_count} msgs\n")
            return True

        # ── Hash arg: resume a specific session ─────────────────────
        msgs = sm.load(arg)
        if msgs:
            ctx["agent"]._state.messages = msgs
            ctx["agent"]._current_session_id = sm.resolve_session_id(arg) or arg
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
        elif arg == "branch" or arg == "branches":
            print(git.list_branches())
        elif arg == "undo":
            ok, out = git.undo_commit()
            print(out)
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


    # ── Review & Fix ────────────────────────────────────────────────────────────
# ── Review & Fix ────────────────────────────────────────────────────

    @r.register("/review", "AI code review of current changes", "review")
    async def cmd_review(arg: str, ctx: dict) -> bool:
        """Multi-dimensional code review of staged + unstaged changes.

        Usage:
            /review            — standard review (all dimensions)
            /review --deep     — thorough review with sub-agent parallelism
            /review --security — security-focused review only
        """
        agent = ctx["agent"]
        git = agent.git

        # Collect diff: staged first, then unstaged, then combine
        staged = git.get_diff(staged=True) if git else ""
        unstaged = git.get_diff(staged=False) if git else ""
        all_diff = (staged or "") + ("\n" + unstaged if unstaged else "")

        if not all_diff or all_diff.strip() in ("", "(no changes)", "(no git repo)"):
            print("No changes to review. Make some edits first! \U0001f4dd")
            return True

        deep = arg.strip().lower() in ("--deep", "-d", "deep")
        security_only = arg.strip().lower() in ("--security", "-s", "security")

        # Truncate diff for the prompt (preserve head + tail if too long)
        diff_for_prompt = all_diff
        if len(diff_for_prompt) > 12000:
            diff_for_prompt = all_diff[:10000] + "\n... (diff truncated, showing first 10k chars)\n"

        # Build dimension-specific review prompts
        dimensions = {
            "bugs": (
                "Find LOGIC BUGS and CORRECTNESS issues: off-by-one errors, null/None "
                "handling, incorrect conditions, missing edge cases, type errors, "
                "race conditions, resource leaks.  IGNORE style and formatting."
            ),
            "security": (
                "Find SECURITY VULNERABILITIES: injection risks, path traversal, "
                "exposed secrets/keys, missing auth checks, unsafe deserialization, "
                "command injection, XSS vectors, insecure defaults. "
                "Flag ANY use of eval/exec/os.system/subprocess with user input."
            ),
            "performance": (
                "Find PERFORMANCE issues: O(n²) or worse algorithms, unnecessary "
                "allocations/copies, missing caching, N+1 query patterns, blocking "
                "I/O on the event loop, excessive memory use, repeated computation."
            ),
            "smells": (
                "Find CODE SMELLS: overly complex functions (>30 lines or deep nesting), "
                "duplicated logic, unclear naming, missing error handling, tight coupling, "
                "god objects, feature envy.  NOT style nitpicks — real design problems."
            ),
        }

        if security_only:
            dimensions = {"security": dimensions["security"]}

        total_lines = all_diff.count("\n")
        print(f"\U0001f50d Reviewing {total_lines} lines across "
              f"{len(dimensions)} dimension{'s' if len(dimensions) > 1 else ''}...\n")

        if deep and hasattr(agent, "_sub_agent_mgr") and agent._sub_agent_mgr:
            # Deep mode: spawn parallel sub-agents per dimension
            import asyncio
            results: dict[str, str] = {}

            async def _review_dim(dim: str, prompt: str) -> tuple[str, str]:
                full_prompt = (
                    f"You are a senior {dim} reviewer. {prompt}\n\n"
                    f"Review this git diff. For each real issue found, output:\n"
                    f"- **Severity**: critical / high / medium / low\n"
                    f"- **File & line**: where the issue is\n"
                    f"- **Problem**: concise explanation\n"
                    f"- **Fix**: actionable suggestion\n\n"
                    f"If you find NO real issues, say 'No {dim} issues found.'\n\n"
                    f"```diff\n{diff_for_prompt}\n```"
                )
                try:
                    result = await agent.run(full_prompt, stream=False)
                    return dim, result or f"No {dim} issues found."
                except Exception:
                    return dim, f"Error reviewing {dim} dimension."

            tasks = [
                _review_dim(dim, prompt) for dim, prompt in dimensions.items()
            ]
            gathered = await asyncio.gather(*tasks)
            for dim, text in gathered:
                results[dim] = text

            # Print structured multi-dimension report
            print("=" * 60)
            print("  \U0001f4cb CODE REVIEW REPORT")
            print("=" * 60)
            for dim in dimensions:
                label = {"bugs": "\U0001f41b Bugs", "security": "\U0001f6e1 Security",
                         "performance": "⚡ Performance", "smells": "\U0001f3ad Code Smells"}.get(dim, dim)
                body = results.get(dim, "Skipped.")
                print(f"\n--- {label} ---")
                print(body[:3000] if body else "No findings.")
            print("\n" + "=" * 60 + "\n  Review complete. /fix to auto-apply suggestions.\n")
        else:
            # Standard mode: single comprehensive review
            dim_section = "\n\n".join(
                f"### {dim.upper()}\n{prompt}" for dim, prompt in dimensions.items()
            )
            task = (
                "You are a senior code reviewer. Review this git diff across "
                f"{len(dimensions)} dimensions. For each finding, specify:\n"
                "- **Severity**: critical / high / medium / low\n"
                "- **File & line**: where\n"
                "- **Problem**: what and why\n"
                "- **Fix**: concrete action\n\n"
                "If a dimension has no issues, say so. Skip style nitpicks.\n\n"
                f"{dim_section}\n\n"
                f"```diff\n{diff_for_prompt}\n```"
            )
            await agent.run(task, stream=True)
        return True

    @r.register("/fix", "AI apply review suggestions", "review")
    async def cmd_fix(arg: str, ctx: dict) -> bool:
        """Auto-apply code review fixes. Targets specific severity if given.

        Usage:
            /fix            — fix all issues
            /fix critical   — only critical severity
            /fix security   — security issues only
        """
        agent = ctx["agent"]
        git = agent.git
        staged = git.get_diff(staged=True) if git else ""
        unstaged = git.get_diff(staged=False) if git else ""
        all_diff = (staged or "") + ("\n" + unstaged if unstaged else "")

        if not all_diff or all_diff.strip() in ("", "(no changes)", "(no git repo)"):
            print("No changes to fix.")
            return True

        focus = (arg.strip().lower() or "all")
        focus_map = {
            "critical": "critical severity only",
            "high": "critical and high severity only",
            "security": "security vulnerabilities only",
            "performance": "performance issues only",
            "bugs": "logic bugs and correctness issues only",
        }
        focus_desc = focus_map.get(focus, f"{focus} severity issues")

        diff_for_prompt = all_diff[:12000]
        task = (
            f"Review this diff and FIX {focus_desc}. Apply changes directly to "
            f"the affected files using write_file or edit_file.\n\n"
            f"Rules:\n"
            f"- Only fix real, confirmed issues — do NOT refactor or restyle\n"
            f"- Each fix must be minimal and targeted\n"
            f"- After each fix, verify you haven't introduced new problems\n\n"
            f"```diff\n{diff_for_prompt}\n```"
        )
        print(f"\U0001f527 Fixing {focus_desc}...\n")
        await agent.run(task, stream=True)
        return True


    # ── Planner ────────────────────────────────────────────────────────────
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
        try:
            tid = int(arg) if arg else 0
        except ValueError:
            tid = 0
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


    # ── Extensions ────────────────────────────────────────────────────────────
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


    # ── Sub-agents ────────────────────────────────────────────────────────────
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

    @r.register("/config", "Show current configuration", "settings")
    def cmd_config(arg: str, ctx: dict) -> bool:
        agent = ctx["agent"]
        cfg = agent.config
        print(f"  Model:       {cfg.llm.model}")
        print(f"  API Base:    {cfg.llm.base_url}")
        print(f"  Workspace:   {cfg.agent.workspace_dir}")
        print(f"  Max Tokens:  {cfg.llm.max_output_tokens}")
        print(f"  Temperature: {cfg.llm.temperature}")
        print(f"  Thinking:    {cfg.llm.thinking_strength}")
        print(f"  Max Context: {cfg.agent.max_context_tokens:,}")
        print(f"  Max Tools:   {cfg.agent.max_tool_calls}")
        print(f"  Session:     {agent.session_id}")
        token_est = agent.get_token_estimate()
        print(f"  Tokens used: ~{token_est:,}")
        print(f"  Anthropic:   {'yes' if getattr(agent, '_use_anthropic', False) else 'no'}")
        if agent.skills and agent.skills.active_skill:
            print(f"  Active skill: {agent.skills.active_skill.name}")
        return True

    @r.register("/status", "Show conversation window", "basic")
    def cmd_status(arg: str, ctx: dict) -> bool:
        agent = ctx["agent"]
        ctx["ui"].show_context(
            total_messages=len(agent._state.messages),
            tool_calls=agent._state.tool_call_count,
            skill=agent.skills.active_skill.name if agent.skills and agent.skills.active_skill else "default",
            model=ctx["config"].llm.model,
            estimated_tokens=agent.get_token_estimate(),
            max_tokens=ctx["config"].agent.max_context_tokens,
        )
        return True

    @r.register("/vision", "Analyze image with multimodal vision", "settings")
    async def cmd_vision(arg: str, ctx: dict) -> bool:
        if not arg:
            print("Usage: /vision <image_path> [prompt]")
            print("  Analyze an image using the configured vision model.")
            print("  Configure in ~/.ata_coder/settings.json:")
            print('    {"vision": {"model": "...", "api_base": "...", "api_key": "..."}}')
            print("  Or set VISION_MODEL / VISION_API_KEY env vars.")
            print("  Falls back to main API config if not set.")
            return True
        parts = arg.split(maxsplit=2)
        image_path = parts[0]
        prompt = parts[1] if len(parts) > 1 else "Describe this image in detail."
        agent = ctx["agent"]
        result = await agent.tools._tool_analyze_image(image_path, prompt)
        if result.success:
            print(result.output)
        else:
            print(f"Error: {result.error}")
        return True

    @r.register("/auto-skill", "Smart skill detection (LLM router)", "skill")
    async def cmd_auto_skill(arg: str, ctx: dict) -> bool:
        if not arg:
            print("Usage: /auto-skill <task description>")
            print("  Uses LLM to intelligently route to the best skill.")
            return True
        agent = ctx["agent"]
        skill_mgr = ctx.get("skill_mgr")
        if not skill_mgr or not agent:
            print("Skills or agent not available.")
            return True
        results = await skill_mgr.detect_skills_smart(arg, max_results=5, llm_client=agent.llm)
        if not results:
            print("No matching skills found.")
            return True
        print(f"Smart routing for: {arg[:80]}...")
        print(f"{'Skill':<22} {'Confidence':>10}")
        print("-" * 34)
        for skill, conf in results:
            bar = "█" * int(conf * 10) + "░" * (10 - int(conf * 10))
            print(f"{skill.name:<22} {bar} {conf:.0%}")
        return True

    # ── Code Generation ─────────────────────────────────────────────────
    @r.register("/test", "Auto-generate unit tests for a file or diff", "generate")
    async def cmd_test(arg: str, ctx: dict) -> bool:
        """Generate pytest unit tests for a file or the current git diff.

        Usage:
            /test                  — generate tests for current git changes
            /test path/to/file.py  — generate tests for a specific file
            /test --all            — generate tests for all modified files
        """
        agent = ctx["agent"]
        target = arg.strip()

        if target in ("--all", "-a"):
            git = agent.git
            if git:
                changed = git.get_changed_files()
                if not changed:
                    print("No changed files found.")
                    return True
                print(f"\U0001f9ea Generating tests for {len(changed)} files...\n")
                for fpath in changed:
                    if fpath.endswith(".py") and not fpath.startswith("test"):
                        await agent.run(
                            f"Read {fpath}, analyze its functions/classes, then "
                            f"write pytest unit tests to tests/test_{Path(fpath).stem}.py. "
                            f"Cover edge cases, happy paths, and error handling. "
                            f"Use pytest fixtures where appropriate.",
                            stream=True,
                        )
                return True
            print("No git repo detected. Use /test <file_path> instead.")
            return True

        if not target:
            # Default: generate tests for current git diff
            git = agent.git
            diff_text = git.get_diff() if git else ""
            if not diff_text or diff_text.strip() in ("", "(no changes)"):
                print("No changes to test. Specify a file: /test <path>")
                return True

            changed_files = git.get_changed_files() if git else []
            py_files = [f for f in changed_files if f.endswith(".py") and "test" not in f]
            if not py_files:
                print("No Python source files in the diff.")
                return True

            print(f"\U0001f9ea Generating tests for changed files: {', '.join(py_files[:5])}\n")
            for fpath in py_files[:3]:
                await agent.run(
                    f"Read {fpath}, then write comprehensive pytest unit tests "
                    f"to tests/test_{Path(fpath).stem}.py. Cover all new/changed "
                    f"functions, their edge cases, error paths, and typical usage. "
                    f"Match the existing project test style.",
                    stream=True,
                )
            return True

        # Specific file path provided
        fpath = Path(target)
        if not fpath.exists():
            print(f"File not found: {target}")
            return True
        print(f"\U0001f9ea Generating tests for: {target}\n")
        test_path = f"tests/test_{fpath.stem}.py"
        await agent.run(
            f"Read {target} thoroughly, then write pytest unit tests to {test_path}. "
            f"Cover every function and class, including edge cases, type errors, "
            f"and boundary conditions. Use pytest fixtures and parametrize where helpful. "
            f"Match the existing project test conventions.",
            stream=True,
        )
        return True

    @r.register("/doc", "Auto-generate or update docstrings", "generate")
    async def cmd_doc(arg: str, ctx: dict) -> bool:
        """Generate or update docstrings (Google-style) for Python code.

        Usage:
            /doc                  — add docstrings to functions in git diff
            /doc path/to/file.py  — add docstrings to a specific file
            /doc --all            — update all docstrings in changed files
        """
        agent = ctx["agent"]
        target = arg.strip()

        if target in ("--all", "-a"):
            git = agent.git
            if git:
                changed = git.get_changed_files()
                py_files = [f for f in changed if f.endswith(".py") and "test" not in f]
                if not py_files:
                    print("No Python source files changed.")
                    return True
                print(f"\U0001f4dd Generating docstrings for {len(py_files)} files...\n")
                for fpath in py_files[:5]:
                    await agent.run(
                        f"Read {fpath}. For every function and class that is missing "
                        f"or has an incomplete docstring, add a Google-style docstring "
                        f"with Args, Returns, and Raises sections. Do NOT modify any "
                        f"code logic — only add/improve docstrings.",
                        stream=True,
                    )
                return True
            print("No git repo. Use /doc <file_path> instead.")
            return True

        if not target:
            git = agent.git
            diff = git.get_diff() if git else ""
            if not diff or diff.strip() in ("", "(no changes)"):
                print("No changes. Use /doc <path> to document a specific file.")
                return True
            changed = git.get_changed_files() if git else []
            py_files = [f for f in changed if f.endswith(".py") and "test" not in f]
            if not py_files:
                print("No Python files changed.")
                return True
            print(f"\U0001f4dd Documenting: {', '.join(py_files[:5])}\n")
            for fpath in py_files[:3]:
                await agent.run(
                    f"Read {fpath}. Add or improve Google-style docstrings for every "
                    f"new or changed function/class. Include Args, Returns, Raises. "
                    f"Do NOT modify any code logic.",
                    stream=True,
                )
            return True

        fpath = Path(target)
        if not fpath.exists():
            print(f"File not found: {target}")
            return True
        print(f"\U0001f4dd Documenting: {target}\n")
        await agent.run(
            f"Read {target}. Add or improve Google-style docstrings for ALL "
            f"functions, methods, and classes. Use the format:\n"
            f"    \"\"\"Summary line.\n\n"
            f"    Args:\n        name: description.\n"
            f"    Returns:\n        description.\n"
            f"    Raises:\n        ExceptionType: when.\n"
            f"    \"\"\"\n"
            f"Do NOT modify any code logic — only add/improve docstrings.",
            stream=True,
        )
        return True

    @r.register("/rag", "Search codebase with RAG semantic search", "generate")
    def cmd_rag(arg: str, ctx: dict) -> bool:
        """Semantic search over the entire codebase using RAG.

        Usage:
            /rag <query>              — search codebase semantically
            /rag --index              — rebuild the RAG index
            /rag --stats              — show RAG index statistics
        """
        if not arg:
            print("Usage: /rag <query>  — search the codebase")
            print("       /rag --index   — rebuild the search index")
            print("       /rag --stats   — show index stats")
            return True

        from ..rag_memory import get_rag_index

        agent = ctx["agent"]
        ws = str(getattr(agent.tools, "workspace", "."))

        if arg.strip() == "--index":
            print("Indexing project files...")
            rag = get_rag_index(ws)
            count = rag.index_project(force=True)
            print(f"Indexed {count} code chunks across the project.")
            return True

        if arg.strip() == "--stats":
            rag = get_rag_index(ws)
            if not rag._indexed:
                rag.index_project()
            print(f"RAG Index: {rag.chunk_count} chunks indexed")
            print(f"Workspace: {ws}")
            emb_status = "ready" if rag._embedder else "not loaded (pip install sentence-transformers for better results)"
            print(f"Embeddings: {emb_status}")
            return True

        # Search
        rag = get_rag_index(ws)
        if not rag._indexed:
            print("Indexing project (first time, may take a moment)...")
            rag.index_project()

        results = rag.search(arg.strip(), top_k=8)
        if not results:
            print(f"No results for: {arg}")
            return True

        print(f"\U0001f50d RAG search: '{arg}' ({len(results)} results)\n")
        for i, sr in enumerate(results[:8], 1):
            c = sr.chunk
            print(f"  [{i}] {c.file_path}:{c.start_line} ({c.kind}: {c.name}) "
                  f"[{sr.match_type} {sr.score:.2f}]")
            preview = c.content[:150].replace("\n", " ").strip()
            print(f"      {preview}...\n")
        return True

    return r

