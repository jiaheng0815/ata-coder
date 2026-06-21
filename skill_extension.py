# -*- coding: utf-8 -*-
"""
SkillExtension — adapter that wraps a Skill as an Extension.

Lets the ExtensionManager manage Skills and non-Skill extensions
uniformly, enabling multi-skill + extension coexistence.
"""

import logging
from typing import TYPE_CHECKING

from .extension import Extension, ExtensionMeta

if TYPE_CHECKING:
    from .skills import Skill

logger = logging.getLogger(__name__)

__all__ = ["SkillExtension"]


class SkillExtension(Extension):
    """
    Adapter: wraps a Skill (from skills.py) as an Extension.

    Skill.system_prompt → get_prompt()
    Skill.tools (list[str]) → get_tools() (list of tool names)
    Skill.triggers are NOT mapped (SkillManager handles detection separately).

    Usage:
        from .skills import Skill
        from .skill_extension import SkillExtension

        skill = Skill(name="debugger", ...)
        ext = SkillExtension(skill)
        manager.register(ext)
        manager.activate("skill:debugger")
    """

    def __init__(self, skill: "Skill"):
        """
        Args:
            skill: skills.Skill instance
        """
        self._skill = skill
        self.meta = ExtensionMeta(
            name=f"skill:{skill.name}",
            version="1.0.0",
            description=skill.description or f"Skill: {skill.name}",
            tags=["skill", skill.name],
            priority=80,  # Skills have higher priority than general extensions (default 100)
        )
        # Skill-to-skill dependencies (Python packages go in requirements.txt instead)
        if hasattr(skill, "dependencies") and skill.dependencies:
            self.meta.dependencies = list(skill.dependencies)

    @property
    def skill_name(self) -> str:
        """The raw skill name (without the 'skill:' prefix)."""
        return self._skill.name

    # ── Extension interface ─────────────────────────────────────────────────

    def get_prompt(self) -> str:
        """返回 skill 的 system prompt（含 @include 解析）。"""
        return self._skill.resolve_includes(self._skill.system_prompt or "")

    def get_tools(self) -> list[str]:
        """
        返回 skill 限制的工具名称列表。
        空列表 = 允许所有工具。
        """
        if hasattr(self._skill, "tools") and self._skill.tools:
            return list(self._skill.tools)
        return []

    def on_activate(self) -> None:
        """Skill 被激活时的回调。"""
        logger.info("Skill activated via extension: %s", self._skill.name)

    def on_deactivate(self) -> None:
        """Skill 被停用时的回调。"""
        logger.info("Skill deactivated via extension: %s", self._skill.name)

    def validate(self) -> tuple[bool, str]:
        """验证 skill 是否可用。"""
        if not self._skill.system_prompt:
            return False, "Skill has no system_prompt"
        return True, "OK"

    def __repr__(self) -> str:
        return f"SkillExtension(name={self._skill.name!r})"
