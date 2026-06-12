# -*- coding: utf-8 -*-
"""
SkillExtension — 将 Skill 包装为 Extension 的适配器。

让 ExtensionManager 统一管理 Skills 和非 Skill Extension，
实现多 skill + extension 的共存。
"""

import logging
from typing import TYPE_CHECKING, Any

from .extension import Extension, ExtensionMeta

if TYPE_CHECKING:
    from .skills import Skill

logger = logging.getLogger(__name__)

__all__ = ["SkillExtension"]


class SkillExtension(Extension):
    """
    适配器：包装 Skill (from skills.py) 作为 Extension。

    Skill 的 system_prompt 映射到 get_prompt(),
    Skill 的 tools 列表映射到 get_tools() 返回的工具名称列表,
    Skill 的 triggers 不映射（由 SkillManager 单独处理检测逻辑）。

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
            skill: skills.Skill 实例
        """
        # 延迟导入避免循环依赖
        self._skill = skill
        self.meta = ExtensionMeta(
            name=f"skill:{skill.name}",
            version="1.0.0",
            description=skill.description or f"Skill: {skill.name}",
            tags=["skill", skill.name],
            priority=80,  # Skills have higher priority than general extensions (default 100)
        )
        # 如果 skill 有依赖，也带过来
        if hasattr(skill, "dependencies") and skill.dependencies:
            self.meta.dependencies = [
                f"skill:{d}" if not d.startswith("skill:") else d
                for d in skill.dependencies
            ]

    @property
    def skill_name(self) -> str:
        """原始 skill 名称（不带 'skill:' 前缀）。"""
        return self._skill.name

    # ── Extension 接口 ────────────────────────────────────────────────────

    def get_prompt(self) -> str:
        """返回 skill 的 system prompt。"""
        return self._skill.system_prompt or ""

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
