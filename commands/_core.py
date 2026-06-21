"""Command handlers — auto-split from commands.py."""

from __future__ import annotations
from typing import Any


def register_commands(r: Any) -> None:
    """Register this group's commands on the registry."""
    # ── Basic ────────────────────────────────────────────────────────────
# ── Basic ──────────────────────────────────────────────────────────

    @r.register("/help", "Show help", "basic")
    def cmd_help(arg: str, ctx: dict) -> bool:
        ctx["ui"].show_help()
        return True

    @r.register("/quit", "Exit", "basic")
    @r.register("/exit", "Exit", "basic")
    @r.register("/q", "Exit", "basic")
    def cmd_quit(arg: str, ctx: dict) -> bool:
        agent = ctx.get("agent")
        sid = getattr(agent, "_current_session_id", "") if agent else ""
        if sid and "-" in sid:
            parts = sid.split("-")
            if len(parts) >= 2:
                print(f"\nResume this session with:\n  ata --resume {parts[-1]}")
                return False
        print("Goodbye!")
        return False

    @r.register("/clear", "Clear conversation", "basic")
    def cmd_clear(arg: str, ctx: dict) -> bool:
        ctx["agent"].reset()
        print("Conversation cleared.")
        return True

    @r.register("/context", "Show conversation window", "basic")
    def cmd_context(arg: str, ctx: dict) -> bool:
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

    @r.register("/compact", "Compact conversation", "basic")
    async def cmd_compact(arg: str, ctx: dict) -> bool:
        result = await ctx["agent"].compact()
        print(result)
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


    # ── Skills ────────────────────────────────────────────────────────────
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
        elif arg.strip() == "":
            state = ctx["auto_skill_state"]["value"]
            print(f"Auto-skill: {'on' if state else 'off'}")
        else:
            ctx["auto_skill_state"]["value"] = True
            print("Auto-skill: on")
        return True


    # ── Memory ────────────────────────────────────────────────────────────
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

        from ..memory import Memory
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
        # Invalidate the agent's cached system prompt so the next
        # LLM call reflects the memory change.
        agent = ctx.get("agent")
        if agent:
            agent._cached_system_prompt = None
        return True

    @r.register("/remember-suggestion", "Save a memory suggestion", "memory")
    def cmd_remember_suggestion(arg: str, ctx: dict) -> bool:
        agent = ctx.get("agent")
        if not agent:
            print("Agent not available.")
            return True
        suggestions = getattr(agent, '_pending_memory_suggestions', [])
        if not suggestions:
            print("No pending memory suggestions.")
            return True
        try:
            idx = int(arg.strip()) - 1
        except ValueError:
            print("Usage: /remember-suggestion <n> (e.g. /remember-suggestion 1)")
            return True
        if idx < 0 or idx >= len(suggestions):
            print(f"Invalid index. Choose 1-{len(suggestions)}.")
            return True
        # Create a memory from the suggestion
        from ..memory import Memory
        text = suggestions[idx]
        name = text.replace(' ', '-').replace(':', '').lower()[:64]
        m = Memory(
            name=name,
            description=text[:200],
            content=text,
            metadata={"type": "reference"},
        )
        store = ctx.get("memory_store")
        if store:
            store.add(m)
            print(f"Saved: {text[:120]}")
        # Remove from pending
        agent._pending_memory_suggestions.pop(idx)
        if agent:
            agent._cached_system_prompt = None
        return True

    @r.register("/dismiss-suggestion", "Dismiss a memory suggestion", "memory")
    def cmd_dismiss_suggestion(arg: str, ctx: dict) -> bool:
        agent = ctx.get("agent")
        if not agent:
            print("Agent not available.")
            return True
        suggestions = getattr(agent, '_pending_memory_suggestions', [])
        if not suggestions:
            print("No pending memory suggestions.")
            return True
        try:
            idx = int(arg.strip()) - 1
        except ValueError:
            print("Usage: /dismiss-suggestion <n> (e.g. /dismiss-suggestion 1)")
            return True
        if idx < 0 or idx >= len(suggestions):
            print(f"Invalid index. Choose 1-{len(suggestions)}.")
            return True
        removed = agent._pending_memory_suggestions.pop(idx)
        print(f"Dismissed: {removed[:120]}")
        return True

    @r.register("/checkpoint", "Save a conversation checkpoint", "memory")
    def cmd_checkpoint(arg: str, ctx: dict) -> bool:
        """Save the current conversation state as a named checkpoint.

        Usage:
            /checkpoint                  — auto-named checkpoint
            /checkpoint fixed-auth-bug   — named checkpoint
    """
        from ..memory_project import ProjectMemory

        agent = ctx["agent"]
        store = ctx.get("memory_store")
        ws = str(getattr(agent.tools, "workspace", "."))
        pm = ProjectMemory(store=store, workspace_dir=ws)

        summary = arg.strip() if arg.strip() else "Manual checkpoint"
        msg_count = len(agent._state.messages)
        tool_count = agent._state.tool_call_count

        cid = pm.save_checkpoint(
            summary=summary,
            message_count=msg_count,
            tool_call_count=tool_count,
            tags=[arg.strip()] if arg.strip() else [],
        )
        print(f"✅ Checkpoint saved: {cid}")
        print(f"   Project: {pm.project_name}")
        print(f"   Messages: {msg_count}, Tool calls: {tool_count}")
        print(f"   Resume with: ata --resume {agent.session_id[-8:] if hasattr(agent, 'session_id') else '?'}")
        return True

    @r.register("/checkpoints", "List saved checkpoints", "memory")
    def cmd_checkpoints(arg: str, ctx: dict) -> bool:
        """List all checkpoints for the current project."""
        from ..memory_project import ProjectMemory

        agent = ctx["agent"]
        store = ctx.get("memory_store")
        ws = str(getattr(agent.tools, "workspace", "."))
        pm = ProjectMemory(store=store, workspace_dir=ws)

        checkpoints = pm.list_checkpoints(limit=20)
        if not checkpoints:
            print("No checkpoints saved yet for this project.")
            print("Use /checkpoint [name] to save one!")
            return True

        print(f"\U0001f4cb Checkpoints for {pm.project_name}:\n")
        for i, cp in enumerate(checkpoints, 1):
            date = cp.created[:16] if cp.created else "?"
            tags_str = f" [{', '.join(cp.tags)}]" if cp.tags else ""
            print(f"  [{i}] {cp.id} | {date} | {cp.summary[:60]}{tags_str}")
            print(f"      {cp.message_count} msgs, {cp.tool_call_count} tools")
        return True

    @r.register("/project-memory", "Show project-scoped memories", "memory")
    def cmd_project_memory(arg: str, ctx: dict) -> bool:
        """Show memories scoped to the current project."""
        from ..memory_project import ProjectMemory

        agent = ctx["agent"]
        store = ctx.get("memory_store")
        ws = str(getattr(agent.tools, "workspace", "."))
        pm = ProjectMemory(store=store, workspace_dir=ws)

        project_memories = [
            m for m in store.list_all()
            if m.metadata.get("project_id") == pm.project_id
        ]
        if not project_memories:
            print(f"No memories for project: {pm.project_name}")
            print("Use /remember to save project-specific knowledge!")
            return True

        print(f"\U0001f4bc Memories for {pm.project_name}:\n")
        for m in project_memories[:15]:
            mtype = m.metadata.get("type", "?")
            print(f"  [{mtype}] {m.description}")
            if m.content:
                print(f"         {m.content[:120]}...")
        return True

    # ── Plugins ──────────────────────────────────────────────────────────
    @r.register("/plugin", "Manage community plugins", "plugin")
    @r.register("/plugins", "Manage community plugins", "plugin")
    async def cmd_plugin(arg: str, ctx: dict) -> bool:
        """Manage community plugins via agent-powered discovery + install.

        Usage:
            /plugin list                — search PyPI/GitHub for ata-coder plugins
            /plugin search <keyword>    — search for specific plugins
            /plugin install <name>      — install a plugin via pip
            /plugin remove <name>       — uninstall a plugin
            /plugin installed           — show what's already installed
        """
        agent = ctx["agent"]
        parts = arg.strip().split(maxsplit=1)
        action = parts[0].lower() if parts else "list"
        target = parts[1].strip() if len(parts) > 1 else ""

        if action in ("list", "ls"):
            await agent.run(
                "Search the web for ATA Coder community plugins. Check these sources:\n"
                "1. PyPI: Search for packages named 'ata-coder-*' using https://pypi.org/search/?q=ata-coder\n"
                "2. GitHub: Search for repos tagged 'ata-coder-plugin' at https://github.com/topics/ata-coder-plugin\n"
                "List ALL plugins you find with their name, description, version, and install command.\n"
                "Use web_search and web_fetch to get accurate info.",
                stream=True,
            )

        elif action == "search":
            if not target:
                print("Usage: /plugin search <keyword>")
                return True
            await agent.run(
                f"Search for ATA Coder plugins matching '{target}'. "
                f"Check PyPI (pypi.org) and GitHub for relevant plugins. "
                f"Report what you find with install instructions.",
                stream=True,
            )

        elif action == "install":
            if not target:
                print("Usage: /plugin install <name>")
                return True
            await agent.run(
                f"Install the ATA Coder plugin '{target}'. Steps:\n"
                f"1. Search pypi.org for a package named like 'ata-coder-{target}' or '{target}'\n"
                f"2. If found on PyPI, run: pip install <package_name>\n"
                f"3. If NOT on PyPI, search GitHub for a repo named like '{target}' tagged 'ata-coder-plugin'\n"
                f"4. If found on GitHub, clone it and pip install -e <path>\n"
                f"5. After installing, verify it works\n"
                f"Report success or failure clearly.",
                stream=True,
            )

        elif action in ("remove", "uninstall"):
            if not target:
                print("Usage: /plugin remove <name>")
                return True
            await agent.run(
                f"Uninstall the ATA Coder plugin '{target}'. "
                f"Run: pip uninstall -y ata-coder-{target} or pip uninstall -y {target} "
                f"(try both names if needed). Confirm success or report what went wrong.",
                stream=True,
            )

        elif action == "installed":
            await agent.run(
                "List all installed ATA Coder plugins. Run: pip list | grep -i ata-coder "
                "or check for packages with 'ata-coder' in the name using: pip show <name> "
                "for each candidate. Show version numbers and descriptions.",
                stream=True,
            )

        else:
            print("Usage: /plugin [list|search|install|remove|installed]")
        return True



