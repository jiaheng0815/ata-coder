"""
Session persistence — save, list, resume, and export conversations.

Sessions are stored as JSONL files in .ata_coder/sessions/.
Each line is a JSON object representing one message in the conversation.

Session metadata (timestamps, summary, skill used) is stored in a
sessions.json index file.

Usage:
    from .session import SessionManager
    sm = SessionManager()
    sm.save("my-session", agent.state.messages)
    sm.list_sessions()
    messages = sm.load("my-session")
    sm.delete("my-session")
    sm.export_markdown("my-session", "output.md")
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ── Data model ───────────────────────────────────────────────────────────────

@dataclass
class SessionMeta:
    """Metadata about a saved session."""
    id: str
    created: str = ""
    updated: str = ""
    message_count: int = 0
    tool_call_count: int = 0
    summary: str = ""        # first user message, truncated
    skill: str = ""
    model: str = ""
    workspace: str = ""
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created": self.created,
            "updated": self.updated,
            "message_count": self.message_count,
            "tool_call_count": self.tool_call_count,
            "summary": self.summary,
            "skill": self.skill,
            "model": self.model,
            "workspace": self.workspace,
            "tags": self.tags,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionMeta":
        return cls(
            id=d.get("id", ""),
            created=d.get("created", ""),
            updated=d.get("updated", ""),
            message_count=d.get("message_count", 0),
            tool_call_count=d.get("tool_call_count", 0),
            summary=d.get("summary", ""),
            skill=d.get("skill", ""),
            model=d.get("model", ""),
            workspace=d.get("workspace", ""),
            tags=d.get("tags", []),
        )


# ── Session manager ──────────────────────────────────────────────────────────

class SessionManager:
    """
    Manages conversation sessions: save, load, list, delete, export.

    Session storage:
    - .ata_coder/sessions/<id>.jsonl — conversation messages
    - .ata_coder/sessions.json — index of all sessions
    """

    def __init__(self, project_dir: str | Path | None = None):
        if project_dir is None:
            try:
                from .settings import get_settings
                project_dir = get_settings().data_dir
            except Exception:
                project_dir = Path.home() / ".ata_coder"
        self._base_dir = Path(project_dir) / "sessions"
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self._base_dir.parent / "sessions.json"
        self._index: dict[str, SessionMeta] = {}
        self._load_index()

    # ── Index management ─────────────────────────────────────────────────

    def _load_index(self) -> None:
        """Load session index from disk."""
        if self._index_path.exists():
            try:
                with open(self._index_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for item in data.get("sessions", []):
                    meta = SessionMeta.from_dict(item)
                    self._index[meta.id] = meta
                logger.debug("Loaded %d sessions from index", len(self._index))
            except Exception as e:
                logger.warning("Failed to load sessions index: %s", e)

    def _save_index(self) -> None:
        """Save session index to disk."""
        try:
            data = {
                "sessions": [m.to_dict() for m in self._index.values()],
                "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            with open(self._index_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning("Failed to save sessions index: %s", e)

    # ── CRUD ─────────────────────────────────────────────────────────────

    def save(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        summary: str = "",
        skill: str = "",
        model: str = "",
        workspace: str = "",
        tool_call_count: int = 0,
    ) -> SessionMeta:
        """
        Save a session's messages to disk.

        Args:
            session_id: Unique session identifier
            messages: List of OpenAI-format message dicts
            summary: Short description (first user message, truncated)
            skill: Active skill name
            model: Model used
            workspace: Workspace directory
            tool_call_count: Number of tool calls made
        """
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Save messages as JSONL
        session_file = self._base_dir / f"{session_id}.jsonl"
        try:
            with open(session_file, "w", encoding="utf-8") as f:
                for msg in messages:
                    f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error("Failed to save session %s: %s", session_id, e)
            raise

        # Update index
        existing = self._index.get(session_id)
        meta = SessionMeta(
            id=session_id,
            created=existing.created if existing else now,
            updated=now,
            message_count=len(messages),
            tool_call_count=tool_call_count,
            summary=summary[:200] if summary else "",
            skill=skill,
            model=model,
            workspace=workspace,
            tags=existing.tags if existing else [],
        )
        self._index[session_id] = meta
        self._save_index()

        logger.info("Saved session %s: %d messages", session_id, len(messages))
        return meta

    def load(self, session_id: str) -> list[dict[str, Any]] | None:
        """
        Load a session's messages from disk.
        Returns list of message dicts or None if not found.
        """
        session_file = self._base_dir / f"{session_id}.jsonl"
        if not session_file.exists():
            logger.warning("Session not found: %s", session_id)
            return None

        try:
            messages = []
            with open(session_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        messages.append(json.loads(line))
            logger.info("Loaded session %s: %d messages", session_id, len(messages))
            return messages
        except Exception as e:
            logger.error("Failed to load session %s: %s", session_id, e)
            return None

    def delete(self, session_id: str) -> bool:
        """Delete a session (messages + index entry)."""
        session_file = self._base_dir / f"{session_id}.jsonl"
        deleted = False
        if session_file.exists():
            try:
                session_file.unlink()
                deleted = True
            except Exception as e:
                logger.error("Failed to delete session file: %s", e)

        if session_id in self._index:
            del self._index[session_id]
            self._save_index()
            deleted = True

        return deleted

    def get_meta(self, session_id: str) -> SessionMeta | None:
        """Get session metadata."""
        return self._index.get(session_id)

    def list_sessions(self, limit: int = 20) -> list[SessionMeta]:
        """List all sessions, newest first."""
        sessions = sorted(
            self._index.values(),
            key=lambda m: m.updated,
            reverse=True,
        )
        return sessions[:limit]

    def search_sessions(self, query: str) -> list[SessionMeta]:
        """Search sessions by summary text."""
        q = query.lower()
        results = []
        for meta in self._index.values():
            if q in meta.summary.lower() or q in meta.id.lower():
                results.append(meta)
        return sorted(results, key=lambda m: m.updated, reverse=True)

    def tag_session(self, session_id: str, tag: str) -> bool:
        """Add a tag to a session."""
        meta = self._index.get(session_id)
        if not meta:
            return False
        if tag not in meta.tags:
            meta.tags.append(tag)
            self._save_index()
        return True

    # ── Export ───────────────────────────────────────────────────────────

    def export_markdown(self, session_id: str, output_path: str | None = None) -> str | None:
        """
        Export a session as a Markdown file.
        Returns the markdown content, or saves to output_path if provided.
        """
        messages = self.load(session_id)
        if not messages:
            return None

        meta = self.get_meta(session_id)
        lines = [
            f"# Session: {session_id}",
            f"",
            f"- **Created:** {meta.created if meta else 'unknown'}",
            f"- **Model:** {meta.model if meta else 'unknown'}",
            f"- **Skill:** {meta.skill if meta else 'none'}",
            f"- **Messages:** {len(messages)}",
            f"",
            "---",
            "",
        ]

        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")

            if role == "system":
                lines.append(f"<details>\n<summary>System Prompt</summary>\n\n```\n{content[:500]}\n```\n</details>\n")
            elif role == "user":
                lines.append(f"### User\n\n{content}\n")
            elif role == "assistant":
                if content:
                    lines.append(f"### Assistant\n\n{content}\n")
                tool_calls = msg.get("tool_calls", [])
                if tool_calls:
                    for tc in tool_calls:
                        fn = tc.get("function", {})
                        lines.append(f"**Tool:** `{fn.get('name', '?')}`\n")
                        lines.append(f"```json\n{fn.get('arguments', '')}\n```\n")
            elif role == "tool":
                lines.append(f"**Tool Result:**\n```\n{content[:500]}\n```\n")

            lines.append("")

        markdown = "\n".join(lines)

        if output_path:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(markdown)
            logger.info("Exported session to %s", output_path)

        return markdown

    def export_json(self, session_id: str, output_path: str | None = None) -> str | None:
        """Export a session as a single JSON file."""
        messages = self.load(session_id)
        if not messages:
            return None

        meta = self.get_meta(session_id)
        data = {
            "session_id": session_id,
            "metadata": meta.to_dict() if meta else {},
            "messages": messages,
        }
        json_str = json.dumps(data, indent=2, ensure_ascii=False)

        if output_path:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(json_str)

        return json_str


# ── Auto-save helper ─────────────────────────────────────────────────────────

def generate_session_id(task: str, skill: str = "") -> str:
    """
    Generate a human-readable session ID from the task.
    Example: "add-type-hints-to-api-20260608"
    """
    import re
    # Take first ~60 chars of task, sanitize
    slug = task.lower().strip()[:60]
    slug = re.sub(r'[^a-z0-9\s-]', '', slug)
    slug = re.sub(r'\s+', '-', slug)
    slug = slug.strip('-')
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    if skill and skill != "general-coder":
        slug = f"{skill}--{slug}"
    return f"{slug[:80]}-{date_str}"
