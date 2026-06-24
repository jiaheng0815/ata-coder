"""Command handlers — auto-split from commands.py."""

from __future__ import annotations
from typing import Any


def register_commands(r: Any) -> None:
    """Register this group's commands on the registry."""
    # ── Think ────────────────────────────────────────────────────────────
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


    # ── Settings ────────────────────────────────────────────────────────────
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

    @r.register("/effort", "Set effort: low/medium/high/xhigh/max", "settings")
    def cmd_effort(arg: str, ctx: dict) -> bool:
        valid = {"low", "medium", "high", "xhigh", "max"}
        if not arg or arg.lower() not in valid:
            current = getattr(ctx.get("config"), "effort", "medium")
            print(f"Effort: {current}  (low / medium / high / xhigh / max)")
            print("  low    = haiku, 4K tokens, thinking disabled")
            print("  medium = default, 16K tokens, no thinking")
            print("  high   = default, 32K tokens, reasoning_effort=high")
            print("  xhigh  = opus, 48K tokens, reasoning_effort=xhigh")
            print("  max    = opus, 64K tokens, reasoning_effort=max")
            return True
        level = arg.lower()
        ctx["config"].effort = level
        agent = ctx["agent"]
        if level == "low":
            agent.llm.config.max_tokens = 4096
            agent.llm.config.thinking_strength = "off"
        elif level == "medium":
            agent.llm.config.max_tokens = 16384
            agent.llm.config.thinking_strength = ""
        elif level == "high":
            agent.llm.config.max_tokens = 32768
            agent.llm.config.thinking_strength = "high"
        elif level == "xhigh":
            agent.llm.config.max_tokens = 49152
            agent.llm.config.thinking_strength = "xhigh"
        elif level == "max":
            agent.llm.config.max_tokens = 65536
            agent.llm.config.thinking_strength = "max"
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
        # Invalidate cached system prompt so next LLM call picks up the new workspace
        agent._cached_system_prompt = None
        print(f"Workspace: {new_path}")
        return True

    @r.register("/permissions", "Permission rules", "settings")
    def cmd_permissions(arg: str, ctx: dict) -> bool:
        ps = ctx.get("permission_store")
        print(ps.describe() if ps else "Not loaded.")
        return True

    @r.register("/mcp", "MCP status/search/resources", "settings")
    def cmd_mcp(arg: str, ctx: dict) -> bool:
        mcp = ctx.get("mcp_client")
        if not mcp:
            print("MCP not configured.")
            return True

        # Sub-command: /mcp search <keyword>
        if arg.startswith("search"):
            keyword = arg[len("search"):].strip()
            if not keyword:
                print("Usage: /mcp search <keyword>")
                print("  Searches tool names, descriptions, and resource URIs.")
                return True
            tools = mcp.search_tools(keyword, limit=15)
            if tools:
                print(f"\nMCP tools matching '{keyword}':")
                for t in tools:
                    fn = t.get("function", t)
                    print(f"  {fn.get('name', '?')}: {fn.get('description', '')[:100]}")
            else:
                print(f"No MCP tools found for '{keyword}'.")
            return True

        # Sub-command: /mcp resources
        if arg.startswith("resources"):
            keyword = arg[len("resources"):].strip()
            resources = mcp.search_resources(keyword, limit=15) if keyword else mcp.list_resources()
            if resources:
                print(f"\nMCP resources{f' matching {keyword!r}' if keyword else ''}:")
                for r in resources[:20]:
                    uri = r.get("uri", "?")
                    desc = r.get("description", "")
                    print(f"  {uri}: {desc[:100]}")
            else:
                print("No MCP resources found.")
            return True

        for name in mcp.connected_servers:
            count = sum(
                1 for t in mcp.get_tools()
                if t.get("function", t).get("name", "").startswith(f"mcp__{name}__")
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
        # /mcp search and /mcp resources were formerly registered here but
        # could never be dispatched — _parse_command splits on the first
        # space, so "/mcp search foo" → cmd="/mcp", arg="search foo".
        # Search/resources are now sub-commands handled inside cmd_mcp above.
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



