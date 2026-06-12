"""
Agent subsystems container — replaces the loose collection of Optional[X]
parameters in CoderAgent.__init__ with a single structured dataclass.

Each subsystem is independently initialisable; CoderAgent only needs the
ones that are actually provided.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .extension import ExtensionManager
    from .skills import SkillManager
    from .memory import MemoryStore
    from .mcp_client import MCPClient
    from .prompt_template import TemplateManager
    from .permissions import PermissionStore
    from .project import ProjectInfo
    from .session import SessionManager


@dataclass
class AgentSubsystems:
    """Holds all optional subsystems wired into the agent at construction time.

    Any field left as None means that feature is disabled — the agent skips
    the corresponding behaviours (no skill detection, no memory recall, etc.).
    """

    skills: SkillManager | None = None
    memory: MemoryStore | None = None
    mcp: MCPClient | None = None
    templates: TemplateManager | None = None
    permissions: PermissionStore | None = None
    project_info: ProjectInfo | None = None
    sessions: SessionManager | None = None
    extensions: ExtensionManager | None = None

    @property
    def has_skills(self) -> bool:
        return self.skills is not None

    @property
    def has_memory(self) -> bool:
        return self.memory is not None

    @property
    def has_mcp(self) -> bool:
        return self.mcp is not None

    @property
    def has_templates(self) -> bool:
        return self.templates is not None

    @property
    def has_permissions(self) -> bool:
        return self.permissions is not None

    @property
    def has_project_info(self) -> bool:
        return self.project_info is not None

    @property
    def has_sessions(self) -> bool:
        return self.sessions is not None

    @property
    def has_extensions(self) -> bool:
        return self.extensions is not None
