"""
ATA Coder — Library exports.

Usage as a library:
    from ata_coder import CoderAgent, ToolExecutor, get_config

    config = get_config()
    config.llm.api_key = "sk-..."
    config.llm.model = "gpt-4o"

    agent = CoderAgent(config=config)
    response = agent.run("Write a hello world function")
    print(response)

Three interfaces:
    CLI:    python -m ata_coder.main
    API:    python -m ata_coder.server
    Web:    python -m ata_coder.server (then open web_ui.html)
"""

from .agent import CoderAgent, AgentEvent, TextDeltaEvent, ToolCallEvent, ToolResultEvent
from .agent_subsystems import AgentSubsystems
from .config import AppConfig, LLMConfig, AgentConfig, get_config
from .tools import ToolExecutor, TOOL_DEFINITIONS, ToolResult
from .llm_client import LLMClient, Message
from .skills import SkillManager, Skill, get_skill_manager
from .memory import MemoryStore, Memory, get_memory_store, create_memory
from .permissions import PermissionStore, PermissionMode, get_permissions
from .session import SessionManager, generate_session_id
from .project import ProjectDetector, ProjectInfo
from .system_prompt_builder import SystemPromptBuilder
from .model_registry import ModelInfo, get_model_info, get_model_cost, estimate_cost

__version__ = "2.0.0"
__all__ = [
    "CoderAgent",
    "AgentSubsystems",
    "ToolExecutor",
    "LLMClient",
    "SystemPromptBuilder",
    "get_config",
    "get_skill_manager",
    "get_memory_store",
    "get_permissions",
    "create_memory",
    "SessionManager",
    "ProjectDetector",
    "AgentEvent",
    "AppConfig",
    "LLMConfig",
    "AgentConfig",
    "ModelInfo",
    "get_model_info",
    "get_model_cost",
    "estimate_cost",
]
