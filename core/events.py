# -*- coding: utf-8 -*-
"""
Agent event types — produced during CoderAgent.run() and consumed by UI.
Extracted from agent.py to keep modules under 500 lines.
"""

from dataclasses import dataclass, field
from typing import Any

from ..tools import ToolResult


# ── Event types ──────────────────────────────────────────────────────────────

@dataclass
class AgentEvent:
    """Base event."""
    pass


@dataclass
class TextDeltaEvent(AgentEvent):
    text: str


@dataclass
class ToolCallEvent(AgentEvent):
    tool_name: str
    arguments: dict[str, Any]
    source: str = "builtin"  # "builtin" or "mcp"


@dataclass
class ToolResultEvent(AgentEvent):
    tool_name: str
    result: ToolResult
    source: str = "builtin"
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ThinkingEvent(AgentEvent):
    pass


@dataclass
class ReasoningEvent(AgentEvent):
    """The model is thinking/reasoning (DeepSeek R1/v4 thinking mode)."""
    text: str


@dataclass
class SkillChangedEvent(AgentEvent):
    skill_name: str


@dataclass
class ErrorEvent(AgentEvent):
    error: str


@dataclass
class ToolStreamEvent(AgentEvent):
    """Real-time streaming output chunk from a running tool (e.g. run_shell)."""
    tool_name: str
    chunk: str  # incremental output text


@dataclass
class MemorySuggestionEvent(AgentEvent):
    """Pending memory suggestions the user may want to save."""
    suggestions: list[str]  # list of human-readable suggestion strings


@dataclass
class CompleteEvent(AgentEvent):
    total_tool_calls: int
    total_time: float
    estimated_tokens: int = 0  # current conversation window size
