"""Extension management (skills, discovery, hook points) — mixin for CoderAgent."""
import logging
from typing import Any

from .skill_extension import SkillExtension

logger = logging.getLogger(__name__)


class ExtensionMixin:
    """Extension lifecycle: register skills, discover extensions, register hook points.

    Contract (host class: ``CoderAgent``):
        Requires:
        - ``self._ext_mgr`` — ExtensionManager instance
        - ``self._subsystems`` — AgentSubsystems dataclass
        - ``self.config`` — AppConfig instance
        Provides:
        - ``_register_skills_as_extensions()`` — skill → extension adapter
        - ``_discover_extensions()`` — scan user/system extension dirs
        - ``_activate_defaults()`` — activate configured extensions
    """

    # ── Extension management ──────────────────────────────────────────────

    def _register_skills_as_extensions(self) -> None:
        """Register all loaded SkillManager skills as SkillExtension adapters."""
        if not self.subsys.has_skills:
            return
        for skill in self.subsys.skills.list_skills():
            ext = SkillExtension(skill)
            if self.ext_mgr.register(ext):
                logger.debug("Registered skill extension: skill:%s", skill.name)
            else:
                logger.debug("Skill extension already registered: skill:%s", skill.name)

    def _discover_extensions(self) -> None:
        """Discover extensions from configured extension directories."""
        ext_dirs = getattr(self.config.agent, "extension_dirs", [])
        if not ext_dirs:
            return
        for d in ext_dirs:
            loaded = self.ext_mgr.discover(d)
            if loaded:
                logger.debug("Discovered %d extensions in %s", len(loaded), d)

    def _register_extension_points(self) -> None:
        """Register hook points extensions can tap into."""
        self._ep_on_run_start = self.ext_mgr.extension_point(
            "on_agent_run_start",
            "Called when agent.run() starts — (task, skill_name)"
        )
        self._ep_on_run_complete = self.ext_mgr.extension_point(
            "on_agent_run_complete",
            "Called when agent.run() completes — (task, result, tool_call_count)"
        )
        self._ep_on_tool_execute = self.ext_mgr.extension_point(
            "on_tool_execute",
            "Called before each tool execution — (tool_name, arguments)"
        )
        self._ep_on_tool_result = self.ext_mgr.extension_point(
            "on_tool_result",
            "Called after each tool result — (tool_name, result)"
        )
        self._ep_on_system_prompt = self.ext_mgr.extension_point(
            "on_system_prompt_build",
            "Called during system prompt construction — (prompt, task)"
        )
        self._ep_on_model_route = self.ext_mgr.extension_point(
            "on_model_route",
            "Called after model routing — (task, complexity, model)"
        )
        logger.debug(
            "Registered %d extension points",
            len(self.ext_mgr.list_extension_points()),
        )

    def set_sub_agent_manager(self, mgr: Any) -> None:
        """Set the SubAgentManager for spawn_subagent tool support."""
        self._sub_agent_mgr = mgr
