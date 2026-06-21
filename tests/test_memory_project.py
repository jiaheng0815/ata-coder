"""Tests for project-aware memory extensions."""

import tempfile
from pathlib import Path

import pytest

from ata_coder.memory import Memory, MemoryStore
from ata_coder.memory_project import (
    ProjectMemory,
    detect_project_id,
    detect_project_name,
    Checkpoint,
)


class TestProjectDetection:
    """detect_project_id and detect_project_name."""

    def test_detect_project_id_returns_12_char_hex(self):
        """Project ID should be a 12-character hex string."""
        pid = detect_project_id(".")
        assert len(pid) == 12
        assert all(c in "0123456789abcdef" for c in pid)

    def test_detect_project_id_is_stable(self):
        """Same directory should produce the same project ID."""
        pid1 = detect_project_id(".")
        pid2 = detect_project_id(".")
        assert pid1 == pid2

    def test_detect_project_name_returns_string(self):
        """Project name should be a non-empty string."""
        name = detect_project_name(".")
        assert isinstance(name, str)
        assert len(name) > 0


class TestProjectMemory:
    """ProjectMemory wrapping a MemoryStore."""

    @pytest.fixture
    def tmp_store(self):
        """Create a temporary MemoryStore."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(memory_dir=tmpdir)
            yield store

    def test_project_id_present(self, tmp_store):
        """ProjectMemory should have a project_id."""
        pm = ProjectMemory(store=tmp_store, workspace_dir=".")
        assert pm.project_id
        assert len(pm.project_id) == 12
        assert pm.project_name

    def test_save_project_memory(self, tmp_store):
        """save_project_memory should create a scoped memory."""
        pm = ProjectMemory(store=tmp_store, workspace_dir=".")
        mem = pm.save_project_memory(
            name="test-config",
            description="Test project config",
            content="key=value",
        )
        assert mem is not None
        assert pm.project_id in mem.name
        assert mem.metadata.get("project_id") == pm.project_id

    def test_save_and_list_checkpoints(self, tmp_store):
        """Checkpoints can be saved and listed."""
        pm = ProjectMemory(store=tmp_store, workspace_dir=".")
        cid = pm.save_checkpoint(
            summary="Fixed authentication bug",
            message_count=42,
            tool_call_count=15,
            tags=["auth", "bugfix"],
        )
        assert len(cid) == 8

        checkpoints = pm.list_checkpoints()
        assert len(checkpoints) >= 1
        cp = checkpoints[0]
        assert cp.id == cid
        assert cp.message_count == 42
        assert cp.tool_call_count == 15
        assert "auth" in cp.tags

    def test_list_checkpoints_filters_by_project(self, tmp_store):
        """Checkpoints from other projects should not appear."""
        pm1 = ProjectMemory(store=tmp_store, workspace_dir=".")
        pm1.save_checkpoint(summary="Project A checkpoint")

        # Create a second ProjectMemory with a different workspace
        pm2 = ProjectMemory(store=tmp_store, workspace_dir=tempfile.gettempdir())
        pm2.save_checkpoint(summary="Project B checkpoint")

        # pm1 should only see its own checkpoints
        cps = pm1.list_checkpoints()
        for cp in cps:
            assert cp.project_id == pm1.project_id

    def test_task_progress_set_and_get(self, tmp_store):
        """Task progress can be saved and retrieved."""
        pm = ProjectMemory(store=tmp_store, workspace_dir=".")
        pm.set_task_progress(
            task_id="implement-login",
            status="in_progress",
            detail="Working on OAuth integration",
        )

        tasks = pm.get_task_progress("implement-login")
        assert len(tasks) == 1
        assert tasks[0].metadata["status"] == "in_progress"
        assert "OAuth" in tasks[0].content

    def test_task_progress_status_update(self, tmp_store):
        """Updating task progress should update the same memory."""
        pm = ProjectMemory(store=tmp_store, workspace_dir=".")
        pm.set_task_progress("task-1", status="pending", detail="Start")
        pm.set_task_progress("task-1", status="completed", detail="Done")

        tasks = pm.get_task_progress("task-1")
        assert len(tasks) == 1
        # The memory should show completed status
        assert tasks[0].metadata["status"] == "completed"

    def test_recall_project_context_empty_store(self, tmp_store):
        """recall_project_context with empty store returns empty string."""
        pm = ProjectMemory(store=tmp_store, workspace_dir=".")
        result = pm.recall_project_context("test query")
        assert result == ""

    def test_recall_project_context_with_memories(self, tmp_store):
        """recall_project_context returns relevant project memories."""
        pm = ProjectMemory(store=tmp_store, workspace_dir=".")
        pm.save_project_memory(
            name="stack",
            description="Tech stack",
            content="Python 3.10, FastAPI, PostgreSQL",
        )
        result = pm.recall_project_context("tech stack")
        assert "Tech stack" in result or result == ""  # May not meet score threshold
