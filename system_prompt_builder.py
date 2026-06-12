"""
System prompt builder — extracts the ~80-line _build_system_prompt method
from CoderAgent into its own focused class.

Each section of the prompt is a separate method so individual pieces can be
overridden or tested independently.
"""

import logging
import platform
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SystemPromptBuilder:
    """Constructs the full system prompt for the coding agent.

    Composes: skill persona + template + environment info + project context
    + tool listing + MCP servers + memory context + formatting guidance.
    """

    def __init__(self, subsystems: Any, workspace_dir: str | Path,
                 model: str = "", default_prompt: str = ""):
        """
        Args:
            subsystems: AgentSubsystems instance (or compatible object).
            workspace_dir: Agent working directory.
            model: Current LLM model name (for template context).
            default_prompt: Fallback system prompt when no skill is active.
        """
        self.subsys = subsystems
        self.workspace = str(workspace_dir)
        self.model = model
        self.default_prompt = default_prompt

    def build(self, tool_definitions: list[dict], user_input: str = "") -> str:
        """Build the complete system prompt string.

        Call this at the start of each agent run so the prompt reflects
        the current skill, memory, and MCP state.

        Args:
            tool_definitions: Combined built-in + MCP tool list.
            user_input: The user's current task/question.  When non-empty,
                        memory recall is targeted rather than generic.
        """
        prompt = self._get_base_prompt()

        # Template override
        if self.subsys.has_templates:
            template = self.subsys.templates.get("system")
            if template:
                ctx = self._template_context()
                prompt = template.render(ctx)

        prompt += self._environment_section()
        prompt += self._project_section()
        prompt += self._tools_section(tool_definitions)
        prompt += self._mcp_section()
        prompt += self._memory_section(user_input)
        prompt += self._ops_section()
        prompt += self._formatting_section()

        return prompt

    # ── Sections ──────────────────────────────────────────────────────────

    def _get_base_prompt(self) -> str:
        """Obtain the base prompt from active Skills via ExtensionManager, or fallback."""
        # ── Primary path: ExtensionManager aggregates all active skill extensions ──
        if self.subsys.has_extensions and self.subsys.extensions:
            # Get all active skill-tagged extensions
            skill_exts = [
                e for e in self.subsys.extensions.list_active()
                if "skill" in e.meta.tags
            ]
            if skill_exts:
                # Aggregate all skill prompts by priority
                aggregated = self.subsys.extensions.aggregate_prompts(base_prompt="")
                if aggregated.strip():
                    # Trigger extension point for prompt modification
                    if hasattr(self.subsys.extensions, 'extension_point'):
                        ep = self.subsys.extensions.extension_point("on_system_prompt_build")
                        results = ep.trigger(
                            prompt=aggregated, task=getattr(self, '_last_task', "")
                        )
                        # Allow interceptors to replace the prompt
                        for r in results:
                            if r is not None:
                                return r
                    return aggregated

        # ── Fallback: direct skill manager (backward compat) ──────────────────
        if self.subsys.has_skills:
            skill = getattr(self.subsys.skills, 'active_skill', None)
            if skill:
                return skill.system_prompt
            try:
                return self.subsys.skills.get_system_prompt()
            except Exception:
                pass
        return self.default_prompt

    def _environment_section(self) -> str:
        model_line = f"\n- Model: {self.model}" if self.model else ""
        return f"""

## Environment
- Working directory: {self.workspace}
- OS: {platform.system()} {platform.release()}
- Python: {platform.python_version()}{model_line}
- Date: {time.strftime('%Y-%m-%d')}"""

    def _project_section(self) -> str:
        if self.subsys.has_project_info and self.subsys.project_info:
            return "\n" + self.subsys.project_info.to_prompt()
        return ""

    def _tools_section(self, tool_definitions: list[dict]) -> str:
        lines = ["\n## Tools Available"]
        for t in tool_definitions:
            fn = t["function"]
            lines.append(f"\n- **{fn['name']}**: {fn['description'][:100]}")
        return "".join(lines)

    def _mcp_section(self) -> str:
        if not self.subsys.has_mcp:
            return ""
        mcp = self.subsys.mcp
        if mcp.tool_count == 0:
            return ""
        lines = [f"\n\n## MCP Tools ({len(mcp.connected_servers)} servers)"]
        for server in mcp.connected_servers:
            count = sum(
                1 for t in mcp.get_tools()
                if t["function"]["name"].startswith(f"mcp__{server}__")
            )
            lines.append(f"\n- Server: **{server}** ({count} tools)")
        return "".join(lines)

    def _memory_section(self, user_input: str = "") -> str:
        """Return targeted memory recall when *user_input* is available,
        falling back to a compact generic summary so the prompt never bloats."""
        if not self.subsys.has_memory:
            return ""
        mem = self.subsys.memory
        if user_input.strip():
            ctx = mem.recall_context(user_input, max_memories=5)
        else:
            ctx = mem.get_memory_context(max_total=8)
        if ctx:
            return "\n" + ctx
        return ""

    def _ops_section(self) -> str:
        """Operational gotchas — prevents the AI from repeating known failure patterns."""
        return """

## Operational Notes

- **Shell cwd is already the workspace** — you do NOT need `cd` to change directory.
  Compound commands like `cd X && do_Y` work fine, but bare commands already run in the
  project root.
- **If a shell command fails with "not in the allowed list"**, do NOT retry the same
  command. Instead use `python -c "import subprocess; subprocess.run([...], cwd='...')"`
  to run tools that aren't on the PATH, or use full absolute paths.
- **Read error messages carefully** — they tell you exactly what went wrong. Diagnose
  before retrying.
- **Be frugal with context** — file reads are truncated at 2000 lines. Use offset/limit
  to page through large files instead of re-reading the whole thing. Re-reading the same
  file wastes tokens; reference line numbers from earlier reads.
- **When you discover a workaround**, save it with `/remember` so future sessions
  benefit. Example: `/remember ops/shell-workaround "Shell workarounds" | cd fails
  → use python subprocess with cwd kwarg`
- **The context window is auto-compacted at ~200k tokens** (effective attention limit).
  When you see the compaction notice, trust the summary — it captured the key decisions."""

    def _formatting_section(self) -> str:
        return """

## Response Formatting

Your responses should be **clear, structured, and visually scannable**:

- **Bold key terms**: Wrap important words in `**double asterisks**` — file names, function names, concepts, conclusions. Bold acts as visual anchor points for the reader.
- **Structure with headings**: Use `## Summary`, `## Changes`, `## Verification` for complex tasks.
- **Use emojis sparingly** for visual cues: ✅ done, 🐛 bug, ⚡ performance, 🔒 security, ⚠️ warning.
- **Bullet lists** for multiple items, **numbered lists** for steps, **code blocks** (with language tag) for code.
- **Tables** for comparison data (options, before/after, pros/cons).
- **Be concise but complete** — include enough detail to understand, not enough to get lost.
- For **Chinese-speaking users**, respond in Chinese unless they write in English. Mix Chinese explanations with English code identifiers."""

    def _template_context(self) -> Any:
        """Build a TemplateContext for the template renderer."""
        from .prompt_template import TemplateContext
        ctx = TemplateContext({
            "workspace_dir": self.workspace,
            "model": self.model,
        })
        if self.subsys.has_skills:
            skill = self.subsys.skills.active_skill
            if skill:
                ctx.set("skill_name", skill.name)
                ctx.set("skill_description", skill.description)
        if self.subsys.has_memory:
            ctx.set("memory_context", self.subsys.memory.get_memory_context())
        return ctx
