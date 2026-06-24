# -*- coding: utf-8 -*-
"""Tests for the agent module (events, state, agent initialization, routing)."""

import asyncio
import pytest

from ata_coder.agent import (
    AgentEvent,
    AgentState,
    CoderAgent,
    CompleteEvent,
    ErrorEvent,
    ReasoningEvent,
    SkillChangedEvent,
    TextDeltaEvent,
    ThinkingEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from ata_coder.config import AgentConfig, AppConfig
from ata_coder.tools import ToolExecutor, ToolResult


# ═══════════════════════════════════════════════════════════════════════════════
# Fake LLM client — enables testing agent run loop without real API
# ═══════════════════════════════════════════════════════════════════════════════

class FakeLLMClient:
    """Mock LLM client returning canned responses so core Agent logic is tested.

    Without this, ``test_explicit_model_in_run`` and ``test_run_resets_state``
    silently skip in CI (any network error → pytest.skip), leaving the agent
    run loop, routing, and error handling completely untested.
    """

    def __init__(self, response_text: str = "hello", model: str = "deepseek-v4-pro"):
        self._response = {"role": "assistant", "content": response_text}
        # Agent routing inspects llm.config.model — provide a minimal config
        from ata_coder.config import LLMConfig
        self.config = LLMConfig(model=model)

    async def chat(self, messages, tools=None, system_prompt=""):
        """Return a canned assistant response with no tool calls."""
        return dict(self._response)

    async def chat_stream(self, messages, tools=None, system_prompt=""):
        """Yield a single text delta, then stop."""
        yield "text", self._response["content"]


# ═══════════════════════════════════════════════════════════════════════════════
# Agent event types
# ═══════════════════════════════════════════════════════════════════════════════

class TestAgentEvents:
    def test_text_delta(self):
        ev = TextDeltaEvent(text="hello")
        assert ev.text == "hello"
        assert isinstance(ev, AgentEvent)

    def test_tool_call(self):
        ev = ToolCallEvent(tool_name="read_file",
                           arguments={"file_path": "foo.py"})
        assert ev.tool_name == "read_file"
        assert ev.arguments["file_path"] == "foo.py"
        assert ev.source == "builtin"

    def test_tool_call_from_mcp(self):
        ev = ToolCallEvent(tool_name="mcp_tool", arguments={}, source="mcp")
        assert ev.source == "mcp"

    def test_tool_result(self):
        r = ToolResult(success=True, output="ok")
        ev = ToolResultEvent(tool_name="read", result=r, arguments={"x": 1})
        assert ev.result.success
        assert ev.tool_name == "read"
        assert ev.arguments == {"x": 1}

    def test_reasoning_event(self):
        ev = ReasoningEvent(text="thinking...")
        assert "thinking" in ev.text

    def test_skill_changed(self):
        ev = SkillChangedEvent(skill_name="debugger")
        assert ev.skill_name == "debugger"

    def test_error_event(self):
        ev = ErrorEvent(error="something went wrong")
        assert "wrong" in ev.error

    def test_complete_event(self):
        ev = CompleteEvent(total_tool_calls=5, total_time=12.3)
        assert ev.total_tool_calls == 5
        assert ev.total_time == 12.3

    def test_thinking_event_is_agent_event(self):
        assert isinstance(ThinkingEvent(), AgentEvent)


# ═══════════════════════════════════════════════════════════════════════════════
# Agent state
# ═══════════════════════════════════════════════════════════════════════════════

class TestAgentState:
    def test_initial_state(self):
        state = AgentState()
        assert state.messages == []
        assert state.tool_call_count == 0
        assert state.start_time == 0.0

    def test_add_messages(self):
        state = AgentState()
        state.messages.append({"role": "user", "content": "hi"})
        assert len(state.messages) == 1

    def test_increment_tool_count(self):
        state = AgentState()
        state.tool_call_count += 1
        state.tool_call_count += 1
        assert state.tool_call_count == 2


# ═══════════════════════════════════════════════════════════════════════════════
# CoderAgent — initialization
# ═══════════════════════════════════════════════════════════════════════════════

class TestCoderAgentInit:
    def test_basic_creation(self):
        config = AppConfig(agent=AgentConfig(workspace_dir="."))
        agent = CoderAgent(config=config)
        assert agent.config is config
        assert agent.current_model == config.llm.model

    def test_default_config(self):
        agent = CoderAgent()
        assert agent.config is not None

    def test_custom_tool_executor(self):
        executor = ToolExecutor()
        agent = CoderAgent(tool_executor=executor)
        assert agent.tools is executor

    def test_event_callback_set(self):
        config = AppConfig(agent=AgentConfig(workspace_dir="."))
        agent = CoderAgent(config=config)
        events = []

        agent.on_event(lambda e: events.append(e))
        agent._emit(ThinkingEvent())
        assert len(events) == 1
        assert isinstance(events[0], ThinkingEvent)

    def test_no_callback_no_crash(self):
        config = AppConfig(agent=AgentConfig(workspace_dir="."))
        agent = CoderAgent(config=config)
        # Should not raise when no on_event callback is registered
        agent._emit(ThinkingEvent())
        # Test passes if no exception propagates


# ═══════════════════════════════════════════════════════════════════════════════
# CoderAgent — model routing
# ═══════════════════════════════════════════════════════════════════════════════

class TestCoderAgentRouting:
    def test_model_routing_switches(self):
        config = AppConfig(agent=AgentConfig(workspace_dir="."))
        agent = CoderAgent(config=config)
        original = agent.current_model
        # Route to same model — no-op
        agent._route_model(original)
        assert agent.current_model == original

    def test_explicit_model_in_run(self):
        """Test that run() with explicit model bypasses routing.

        Uses FakeLLMClient so the agent run loop is actually tested —
        no silent skip when API is unavailable.
        """
        config = AppConfig(agent=AgentConfig(workspace_dir="."))
        agent = CoderAgent(config=config)
        agent.llm = FakeLLMClient("hello")

        response = asyncio.run(agent.run(
            task="Reply with ONLY the word 'hello' and nothing else.",
            stream=False,
            explicit_model=config.llm.model,
        ))
        assert response is not None
        assert isinstance(response, str)
        assert "hello" in response.lower()

    def test_run_resets_state(self):
        """After run(), tool_call_count should be >= 0 (clean state).

        Uses FakeLLMClient — no silent skip when API is unavailable.
        """
        config = AppConfig(agent=AgentConfig(workspace_dir="."))
        agent = CoderAgent(config=config)
        agent.llm = FakeLLMClient("test")

        asyncio.run(agent.run(
            task="Say ONLY 'test' and stop.",
            stream=False,
            explicit_model=config.llm.model,
        ))
        # FakeLLMClient returns text without tool calls, so count stays 0
        assert agent._state.tool_call_count == 0


# ═══════════════════════════════════════════════════════════════════════════════
# CoderAgent — task classification heuristics
# ═══════════════════════════════════════════════════════════════════════════════

class TestClassifyHeuristics:
    """_ai_classify uses scored heuristics for 60–500 char tasks."""

    # -- Length shortcuts --
    def test_very_short_is_simple(self):
        assert CoderAgent._ai_classify("Fix this bug") == "simple"

    def test_very_long_is_complex(self):
        long_task = "Please analyze the entire codebase and " + "x" * 500
        assert CoderAgent._ai_classify(long_task) == "complex"

    # -- Mixed signals (old approach would fail these) --
    def test_simple_keyword_with_complex_intent(self):
        """'what is' prefix but describes a complex debugging scenario."""
        task = ("what is causing the deadlock in my 8-file async pipeline "
                "with race conditions in the connection pool")
        result = CoderAgent._ai_classify(task)
        # Should NOT be simple despite starting with "what is"
        assert result != "simple"

    def test_complex_keyword_with_simple_task(self):
        """'implement' keyword but task is trivial."""
        task = "implement a hello world function that prints greeting"
        result = CoderAgent._ai_classify(task)
        # Should NOT be complex despite containing "implement"
        assert result != "complex"

    # -- Code references --
    def test_code_blocks_signal_complex(self):
        task = ("please refactor this code ```python\n"
                "def handle(): pass\n``` "
                "also fix the crash in src/auth.py and restructure the api/user.py module")
        result = CoderAgent._ai_classify(task)
        assert result == "complex"

    # -- Bug/error language --
    def test_error_report_is_complex(self):
        task = ("The application crashes with a traceback in handler.py "
                "line 42: KeyError unexpected in the middleware stack")
        result = CoderAgent._ai_classify(task)
        assert result == "complex"

    # -- Multi-step --
    def test_numbered_steps_is_complex(self):
        task = ("1. Create the database migration\n"
                "2. Update the ORM models\n"
                "3. Add API endpoints\n"
                "4. Write the integration tests")
        result = CoderAgent._ai_classify(task)
        assert result == "complex"

    # -- Pure question --
    def test_pure_question_is_simple(self):
        task = "what is the difference between asyncio.gather and asyncio.wait in Python's standard library"
        result = CoderAgent._ai_classify(task)
        assert result == "simple"

    # -- Normal (neither clearly simple nor complex) --
    def test_ambiguous_mid_range(self):
        task = "add a docstring to the calculate_total function in utils.py"
        result = CoderAgent._ai_classify(task)
        assert result in ("simple", "normal")  # should NOT be complex


# ═══════════════════════════════════════════════════════════════════════════════
# CoderAgent — token estimation
# ═══════════════════════════════════════════════════════════════════════════════

class TestCoderAgentTokenEstimate:
    def test_empty_state_estimate(self):
        config = AppConfig(agent=AgentConfig(workspace_dir="."))
        agent = CoderAgent(config=config)
        estimate = agent.get_token_estimate()
        assert estimate >= 0

    def test_estimate_with_messages(self):
        config = AppConfig(agent=AgentConfig(workspace_dir="."))
        agent = CoderAgent(config=config)
        agent._state.messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello world"},
        ]
        estimate = agent.get_token_estimate()
        assert estimate > 0


# ═══════════════════════════════════════════════════════════════════════════════
# CoderAgent — subsystems access
# ═══════════════════════════════════════════════════════════════════════════════

class TestCoderAgentSubsystems:
    def test_subsystems_container_exists(self):
        config = AppConfig(agent=AgentConfig(workspace_dir="."))
        agent = CoderAgent(config=config)
        assert agent.subsys is not None

    def test_subsystems_are_optional(self):
        """Without explicit init, subsystems default to None (disabled)."""
        config = AppConfig(agent=AgentConfig(workspace_dir="."))
        agent = CoderAgent(config=config)
        # skills/memory/mcp/templates/permissions all default to None
        # This is by design — they're enabled only when explicitly set up
        assert not agent.subsys.has_skills
        assert not agent.subsys.has_memory

    def test_subsystems_can_be_disabled(self):
        """Explicit None subsystems means feature is disabled."""
        from ata_coder.agent_subsystems import AgentSubsystems
        subsys = AgentSubsystems()
        assert not subsys.has_skills
        assert not subsys.has_memory
        assert not subsys.has_mcp
        assert not subsys.has_templates

    def test_change_tracker_present(self):
        config = AppConfig(agent=AgentConfig(workspace_dir="."))
        agent = CoderAgent(config=config)
        assert agent.change_tracker is not None

    def test_fool_proof_present(self):
        config = AppConfig(agent=AgentConfig(workspace_dir="."))
        agent = CoderAgent(config=config)
        assert agent.fool_proof is not None

    def test_git_workflow_present(self):
        config = AppConfig(agent=AgentConfig(workspace_dir="."))
        agent = CoderAgent(config=config)
        assert agent.git is not None


# ═══════════════════════════════════════════════════════════════════════════════
# CoderAgent — session ID
# ═══════════════════════════════════════════════════════════════════════════════

class TestCoderAgentSession:
    def test_session_id_is_empty_initially(self):
        config = AppConfig(agent=AgentConfig(workspace_dir="."))
        agent = CoderAgent(config=config)
        assert agent.session_id == ""
