"""Tests for SubAgent and SubAgentManager."""

import pytest

from ata_coder.sub_agent import SubAgent, SubAgentResult
from ata_coder.sub_agent_manager import SubAgentManager
from ata_coder.config import AppConfig


class TestSubAgentResult:
    def test_defaults(self):
        r = SubAgentResult(agent_id="test")
        assert r.agent_id == "test"
        assert r.result is None
        assert r.error is None
        assert r.success is True
        assert r.tool_call_count == 0

    def test_error_result(self):
        r = SubAgentResult(agent_id="err", error="boom", success=False)
        assert not r.success
        assert r.error == "boom"


class TestSubAgent:
    def test_init(self):
        config = AppConfig.load()
        sub = SubAgent(config=config, skill_prompt="You are helpful.")
        assert sub.id.startswith("sub_")
        assert sub.status == "idle"
        assert not sub.is_running()
        assert not sub.is_done()

    @pytest.mark.asyncio
    async def test_cancel_idle(self):
        config = AppConfig.load()
        sub = SubAgent(config=config)
        await sub.cancel()
        # Cancel on an idle agent sets done but doesn't change status
        assert sub.is_done()

    @pytest.mark.asyncio
    async def test_wait_not_started(self):
        config = AppConfig.load()
        sub = SubAgent(config=config)
        result = await sub.wait(timeout=0.1)
        assert not result.success
        assert "never started" in (result.error or "")


class TestSubAgentManager:
    def test_init(self):
        config = AppConfig.load()
        mgr = SubAgentManager(config, max_concurrent=3)
        assert mgr.active_count == 0
        assert mgr.total_count == 0

    def test_list_empty(self):
        config = AppConfig.load()
        mgr = SubAgentManager(config)
        assert mgr.list_all() == []
        assert mgr.list_active() == []

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_max_concurrent(self):
        config = AppConfig.load()
        mgr = SubAgentManager(config, max_concurrent=1)
        aid = await mgr.spawn("test task")
        assert aid.startswith("sub_")
        # Second spawn should fail due to limit
        with pytest.raises(RuntimeError):
            await mgr.spawn("another task")

    @pytest.mark.asyncio
    async def test_cancel_all(self):
        config = AppConfig.load()
        mgr = SubAgentManager(config, max_concurrent=3)
        await mgr.spawn("task 1")
        await mgr.spawn("task 2")
        await mgr.cancel_all()
        assert mgr.active_count == 0

    @pytest.mark.asyncio
    async def test_clear_finished(self):
        config = AppConfig.load()
        mgr = SubAgentManager(config)
        await mgr.spawn("task")
        await mgr.cancel_all()
        removed = mgr.clear_finished()
        assert removed == 1
        assert mgr.total_count == 0

    @pytest.mark.asyncio
    async def test_shutdown(self):
        config = AppConfig.load()
        mgr = SubAgentManager(config)
        await mgr.spawn("task")
        await mgr.shutdown()
        assert mgr.total_count == 0
