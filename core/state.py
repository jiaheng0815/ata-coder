# -*- coding: utf-8 -*-
"""
Agent state — holds conversation messages, phase, and metrics during a run.
Extracted from agent.py.
"""

from dataclasses import dataclass, field
from enum import Enum, auto

from ..types import Message


class AgentPhase(Enum):
    """Explicit named phases for the agent's lifecycle.

    Replaces the implicit state transitions scattered through _run_loop()
    and run().  Every phase transition is logged and visible to observers
    (REPL, Clawd, server frontend).
    """
    IDLE = auto()               # waiting for run() to be called
    INITIALIZING = auto()       # inside run(): routing, skill, prompt build
    THINKING = auto()           # waiting for LLM response
    COMPACTING = auto()         # context compaction in progress
    TOOL_EXECUTING = auto()     # executing tool calls (serial or parallel)
    COMPLETED = auto()          # loop exited normally (no tool calls)
    MEMORY_SUGGESTING = auto()  # post-conversation memory suggestions
    ERROR = auto()              # fatal / unrecoverable error
    SHUTDOWN = auto()           # cleanup and resource release


@dataclass
class AgentState:
    """Mutable state tracked across a single agent run."""
    messages: list[Message] = field(default_factory=list)
    tool_call_count: int = 0
    start_time: float = 0.0
    phase: AgentPhase = AgentPhase.IDLE
    consecutive_failures: int = 0
    safety_limit_reached: bool = False
    last_error: str = ""
