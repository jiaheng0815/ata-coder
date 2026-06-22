"""
Project-aware memory extensions for ATA Coder.

Extends the persistent memory system with:
- Project identity detection (git remote + directory name)
- Project-scoped memory recall (only load memories relevant to this project)
- Session checkpoint: save/restore conversation snapshots tagged to a project
- Task progress: track what's in-progress across sessions

Usage:
    from .memory_project import ProjectMemory

    pm = ProjectMemory(store, workspace_dir="...")
    pm.save_checkpoint(agent_state)          # save current state
    pm.recall_project_context(user_input)     # get project-scoped context
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .memory import Memory, MemoryStore

logger = logging.getLogger(__name__)


# ── Project identity ──────────────────────────────────────────────────────────

def detect_project_id(workspace_dir: str | Path) -> str:
    """Derive a stable project identifier from a git remote URL + directory name.

    Returns a short hash (12 hex chars) that uniquely identifies the project.
    Falls back to a hash of the directory path if no git remote is found.
    """
    cwd = Path(workspace_dir).resolve()
    remote_url = ""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(cwd), capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            remote_url = result.stdout.strip()
    except Exception:
        pass

    identity = remote_url or str(cwd)
    return hashlib.sha256(identity.encode()).hexdigest()[:12]


def detect_project_name(workspace_dir: str | Path) -> str:
    """Derive a human-readable project name."""
    cwd = Path(workspace_dir).resolve()
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(cwd), capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            # Extract repo name from URL: owner/repo.git → repo
            name = url.rstrip("/").split("/")[-1]
            if name.endswith(".git"):
                name = name[:-4]
            return name
    except Exception:
        pass
    return cwd.name


# ── Checkpoint dataclass ─────────────────────────────────────────────────────

@dataclass
class Checkpoint:
    """A saved conversation snapshot tied to a project."""
    id: str                          # unique checkpoint ID
    project_id: str                  # which project this belongs to
    summary: str                     # one-line summary of the conversation state
    message_count: int = 0
    tool_call_count: int = 0
    created: str = ""                # ISO timestamp
    tags: list[str] = field(default_factory=list)


# ── ProjectMemory manager ────────────────────────────────────────────────────

class ProjectMemory:
    """Project-scoped memory with checkpoint and task-progress support.

    Wraps a MemoryStore and adds project-awareness: memories tagged with
    a project_id are scoped to the current working directory's git identity.
    """

    def __init__(
        self,
        store: MemoryStore | None = None,
        workspace_dir: str | Path = ".",
    ):
        self._store = store
        self._workspace = str(Path(workspace_dir).resolve())
        self.project_id = detect_project_id(self._workspace)
        self.project_name = detect_project_name(self._workspace)

    @property
    def store(self) -> MemoryStore:
        if self._store is None:
            from .memory import get_memory_store
            self._store = get_memory_store()
        return self._store

    # ── Project-scoped memory operations ──────────────────────────────────

    def save_project_memory(
        self, name: str, description: str, content: str,
        memory_type: str = "project",
    ) -> Memory:
        """Save a memory scoped to the current project."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        mem = Memory(
            name=f"proj-{self.project_id}-{name}",
            description=f"[{self.project_name}] {description}",
            content=content,
            metadata={
                "type": memory_type,
                "project_id": self.project_id,
                "project_name": self.project_name,
                "workspace": self._workspace,
            },
            created=now,
            updated=now,
        )
        return self.store.add(mem)

    def recall_project_context(self, user_input: str = "",
                                max_memories: int = 6) -> str:
        """Recall memories relevant to this project and the user's input.

        Only returns memories tagged with the current project_id or
        global (unscoped) memories.
        """
        self.store._ensure_all_loaded()
        if not self.store._memories:
            return ""

        query = f"{self.project_name} {user_input}" if user_input else self.project_name
        scored = self.store._search_scored(query)

        # Prioritize project-scoped memories, include global ones
        relevant: list[str] = []
        for score, mem in scored:
            pid = mem.metadata.get("project_id", "")
            if pid and pid != self.project_id:
                continue  # Skip memories from other projects
            if len(relevant) >= max_memories:
                break
            if score >= 2.0:  # minimum relevance threshold
                desc = mem.description
                body = mem.content[:300]
                relevant.append(f"- {desc}\n  {body}")

        if not relevant:
            return ""

        return (
            f"\n## Project: {self.project_name}\n" +
            "\n".join(relevant)
        )

    # ── Session checkpoints ───────────────────────────────────────────────

    def save_checkpoint(
        self,
        summary: str,
        message_count: int = 0,
        tool_call_count: int = 0,
        tags: list[str] | None = None,
    ) -> str:
        """Save a conversation checkpoint for later resumption.

        Returns the checkpoint ID (8-char hash).
        """
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cid = hashlib.sha256(
            f"{self.project_id}{now}{summary}".encode()
        ).hexdigest()[:8]

        content_parts = [
            f"Project: {self.project_name}",
            f"Workspace: {self._workspace}",
            f"Messages: {message_count}",
            f"Tool calls: {tool_call_count}",
        ]
        if tags:
            content_parts.append(f"Tags: {', '.join(tags)}")

        mem = Memory(
            name=f"checkpoint-{cid}",
            description=f"Session checkpoint: {summary[:100]}",
            content="\n".join(content_parts),
            metadata={
                "type": "checkpoint",
                "project_id": self.project_id,
                "project_name": self.project_name,
                "checkpoint_id": cid,
                "message_count": message_count,
                "tool_call_count": tool_call_count,
                "tags": tags or [],
            },
            created=now,
            updated=now,
        )
        self.store.add(mem)
        logger.info("Checkpoint saved: %s (%d msgs)", cid, message_count)
        return cid

    def list_checkpoints(self, limit: int = 10) -> list[Checkpoint]:
        """List recent checkpoints for this project."""
        self.store._ensure_all_loaded()
        result: list[Checkpoint] = []
        for mem in self.store._memories.values():
            if mem.metadata.get("type") != "checkpoint":
                continue
            if mem.metadata.get("project_id") != self.project_id:
                continue
            result.append(Checkpoint(
                id=mem.metadata.get("checkpoint_id", mem.name),
                project_id=self.project_id,
                summary=mem.description.replace("Session checkpoint: ", ""),
                message_count=mem.metadata.get("message_count", 0),
                tool_call_count=mem.metadata.get("tool_call_count", 0),
                created=mem.created,
                tags=mem.metadata.get("tags", []),
            ))
        result.sort(key=lambda c: c.created, reverse=True)
        return result[:limit]

    # ── Task progress tracking ────────────────────────────────────────────

    def set_task_progress(self, task_id: str, status: str, detail: str = "") -> Memory:
        """Record the current task's progress (persists across sessions).

        Args:
            task_id: Unique task identifier (e.g. "implement-auth")
            status: One of pending / in_progress / blocked / completed
            detail: Human-readable progress note
        """
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        mem = Memory(
            name=f"task-{self.project_id}-{task_id}",
            description=f"Task [{status}]: {detail[:100] if detail else task_id}",
            content=f"Status: {status}\nDetail: {detail}\nProject: {self.project_name}",
            metadata={
                "type": "task_progress",
                "project_id": self.project_id,
                "task_id": task_id,
                "status": status,
            },
            created=now,
            updated=now,
        )
        return self.store.add(mem)

    def get_task_progress(self, task_id: str = "") -> list[Memory]:
        """Get task progress entries for this project.

        Returns all tasks if task_id is empty, otherwise the specific task.
        """
        self.store._ensure_all_loaded()
        result: list[Memory] = []
        for mem in self.store._memories.values():
            if mem.metadata.get("type") != "task_progress":
                continue
            if mem.metadata.get("project_id") != self.project_id:
                continue
            if task_id and mem.metadata.get("task_id") != task_id:
                continue
            result.append(mem)
        result.sort(key=lambda m: m.updated, reverse=True)
        return result
