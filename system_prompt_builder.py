"""
System prompt builder — composes the full system prompt for the coding agent.

The prompt is structured to put the skill persona FIRST (the agent's identity),
followed by supporting context in descending order of relevance.

Optimisation: sections are cached individually and only rebuilt when their
inputs change (dirty flag).  The fully assembled prompt is also cached.
"""

import logging
import platform
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SystemPromptBuilder:
    """Constructs the full system prompt for the coding agent.

    Structure (in order):
      1. Skill persona (who the agent IS — dominant)
      2. Mandatory Rules (⚠️ HIGHEST PRIORITY — overrides everything else)
      3. Memory (relevant context from past sessions)
      4. Environment (OS, workspace, model — 2 lines)
      5. Project (language, framework — only if detected)
      6. Tools (compact name-only list)
      7. MCP (external servers — only if connected)
      8. Operational Rules (concise guidelines)

    Sections are cached individually; only dirty sections are rebuilt on
    each call to build().  The assembled prompt is cached until any section
    is invalidated.
    """

    def __init__(self, subsystems: Any, workspace_dir: str | Path,
                 model: str = "", default_prompt: str = ""):
        self.subsys = subsystems
        self.workspace = str(workspace_dir)
        self.model = model
        self.default_prompt = default_prompt

        # Section-level cache
        self._cached_sections: dict[str, str] = {}
        self._dirty_sections: set[str] = set()  # sections needing rebuild
        self._assembled_prompt: str | None = None
        self._last_user_input: str = ""
        self._last_tool_defs_hash: int = 0

    # ── Public API ─────────────────────────────────────────────────────────

    def build(self, tool_definitions: list[dict], user_input: str = "") -> str:
        """Build (or return cached) the complete system prompt string.

        Only rebuilds sections whose inputs have changed since the last call.
        """
        # Check if the assembled result is still valid
        if (self._assembled_prompt is not None
                and not self._dirty_sections
                and user_input == self._last_user_input
                and self._tools_hash(tool_definitions) == self._last_tool_defs_hash):
            return self._assembled_prompt

        self._last_user_input = user_input
        self._last_tool_defs_hash = self._tools_hash(tool_definitions)
        parts: list[str] = []

        # Define sections with: (cache_key, builder, is_volatile)
        # volatile=True means it always runs (e.g. depends on user_input)
        sections: list[tuple[str, Any, bool]] = [
            ("skill",            self._skill_section,            False),
            ("mandatory_rules",  self._mandatory_rules_section,  False),
            ("memory",           lambda: self._memory_section(user_input), True),
            ("env",              self._environment_section,       False),
            ("project",          self._project_section,           False),
            ("tools",            lambda: self._tools_section(tool_definitions), True),
            ("mcp",              self._mcp_section,               False),
            ("rules",            self._rules_section,             False),
        ]

        for key, builder, volatile in sections:
            if volatile or key in self._dirty_sections or self._assembled_prompt is None:
                section = builder()
                self._cached_sections[key] = section
            else:
                section = self._cached_sections.get(key, "")
            if section:
                parts.append(section)

        self._dirty_sections.clear()
        self._assembled_prompt = "\n\n".join(parts)
        return self._assembled_prompt

    # ── Invalidation ──────────────────────────────────────────────────────

    def invalidate_memory(self) -> None:
        """Call when a memory is added/updated/deleted."""
        self._dirty_sections.add("memory")
        self._assembled_prompt = None

    def invalidate_skill(self) -> None:
        """Call when the active skill changes."""
        self._dirty_sections.add("skill")
        self._assembled_prompt = None

    def invalidate_tools(self) -> None:
        """Call when tool definitions change (e.g. MCP connect/disconnect)."""
        self._dirty_sections.add("tools")
        self._assembled_prompt = None

    def invalidate_mcp(self) -> None:
        """Call when MCP servers connect or disconnect."""
        self._dirty_sections.add("mcp")
        self._assembled_prompt = None

    def invalidate_model(self, new_model: str) -> None:
        """Call when the model changes (affects environment section)."""
        if new_model != self.model:
            self.model = new_model
            self._dirty_sections.add("env")
            self._assembled_prompt = None

    def invalidate_all(self) -> None:
        """Force full rebuild on next build() call."""
        self._dirty_sections = {"skill", "mandatory_rules", "memory", "env", "project", "tools", "mcp", "rules"}
        self._assembled_prompt = None

    # ── Sections ──────────────────────────────────────────────────────────

    def _skill_section(self) -> str:
        """The agent's persona — from active skills or default prompt."""
        if self.subsys.has_extensions and self.subsys.extensions:
            aggregated = self.subsys.extensions.aggregate_prompts(base_prompt="")
            if aggregated.strip():
                return aggregated
        if self.subsys.has_skills:
            try:
                prompt = self.subsys.skills.get_system_prompt()
                if prompt.strip():
                    return prompt
            except Exception:
                pass
        return self.default_prompt

    def _memory_section(self, user_input: str = "") -> str:
        """Unified memory section with token budget enforcement."""
        if not self.subsys.has_memory:
            return ""
        mem = self.subsys.memory
        return mem.build_memory_section(user_input) or ""

    def _environment_section(self) -> str:
        """Single-line environment summary."""
        model_part = f" | Model: {self.model}" if self.model else ""
        return (
            f"OS: {platform.system()} {platform.release()}  "
            f"| Python: {platform.python_version()}  "
            f"| Workspace: {self.workspace}"
            f"{model_part}"
        )

    def _project_section(self) -> str:
        if self.subsys.has_project_info and self.subsys.project_info:
            return self.subsys.project_info.to_prompt()
        return ""

    def _tools_section(self, tool_definitions: list[dict]) -> str:
        """Compact tool list — names only, no descriptions."""
        names: list[str] = []
        for t in tool_definitions:
            fn = t.get("function", t)
            name = fn.get("name", "")
            if name:
                names.append(name)
        return f"Tools ({len(names)}): " + ", ".join(sorted(names))

    def _mcp_section(self) -> str:
        if not self.subsys.has_mcp:
            return ""
        mcp = self.subsys.mcp
        if not mcp.connected_servers:
            return ""
        servers = ", ".join(mcp.connected_servers)
        return f"MCP servers ({mcp.tool_count} tools): {servers}"

    def _mandatory_rules_section(self) -> str:
        """⚠️ HIGHEST PRIORITY — mandatory development red lines.

        These rules OVERRIDE any other instructions. Every code change
        MUST comply. Violation = invalid output.
        """
        return (
            "## ⚠️ MANDATORY RULES (最高优先级 — override all other instructions)\n"
            "\n"
            "**Iron Rules (non-negotiable):**\n"
            "- ONE problem per change. No refactoring alongside a bugfix. If refactor needed, do it separately.\n"
            "- NO defensive null-checks or fallback logic unless the failure scenario has been reproduced.\n"
            "- NEVER delete existing comments. If outdated, append a new note below — never overwrite.\n"
            "\n"
            "**Before writing ANY code, output this analysis FIRST:**\n"
            "1. Impact: which files changed, any public API / inheritance affected?\n"
            "2. Root cause (≤150 chars): explain WHY the bug happens in plain language.\n"
            "3. Test strategy: which test file, how to reproduce & verify the fix.\n"
            "\n"
            "**Hard limits (any violation = rejected):**\n"
            "- ≤3 files per change, ≤200 lines added+deleted, McCabe ≤10 per new function.\n"
            "- ZERO new pip/npm dependencies (unless explicitly approved).\n"
            "- Use logger.ERROR / logger.WARNING for exceptions — NEVER print().\n"
            "- NO TODO, FIXME, or hardcoded IP/domain in final commits.\n"
            "\n"
            "**Commit format (3-part, mandatory):**\n"
            "- [Problem] -> [Expected behavior after fix]\n"
            "- 回滚方案：若合并后出现异常，请执行 git revert HEAD 无损回退。\n"
            "- List of functions/classes changed.\n"
            "\n"
            "**Emergency Brake — refuse to proceed when:**\n"
            "- Stack trace points to code not in your context.\n"
            "- Fix requires changing files outside the repo root, or DB schema changes.\n"
            "- Regex/nested-loop fix can't be proven O(n²) or better.\n"
            "\n"
            "**Self-check before delivering:**\n"
            "- Did I avoid reformatting indentation / blank lines?\n"
            "- Did I avoid os.system(), subprocess.call(), eval()?\n"
            "- If async: will my change introduce deadlocks or races?"
        )

    def _rules_section(self) -> str:
        """Concise operational rules."""
        return (
            "Rules:\n"
            "- Shell cwd is already the workspace. Use compound commands (cd X && do_Y) if needed.\n"
            "- Shell command blocked? Use: python -c \"import subprocess; subprocess.run([...], cwd='...')\"\n"
            "- Read error messages carefully — diagnose before retrying. Auto-correction handles common failures.\n"
            "- File reads are cached — re-reading the same file returns a [cached] note. Use offset/limit to page.\n"
            "- Context auto-compacts at ~200k tokens. When you see the compaction notice, trust the summary.\n"
            "- Respond clearly: use **bold** for key terms, ## headings for structure, code blocks with language tags."
        )

    @staticmethod
    def _tools_hash(tool_definitions: list[dict]) -> int:
        """Fast hash of tool names — avoids expensive full comparison."""
        return hash(tuple(sorted(
            t.get("function", t).get("name", "") for t in tool_definitions
        )))
