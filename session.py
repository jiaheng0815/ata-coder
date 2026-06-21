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
        """Save session index to disk atomically (write-then-rename)."""
        tmp = self._index_path.with_suffix(".tmp")
        try:
            from .utils import sanitize_surrogates
            data = sanitize_surrogates({
                "sessions": [m.to_dict() for m in self._index.values()],
                "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
            # Belt-and-suspenders: json.dumps → encode→decode strips any
            # lone surrogates that json.dumps(ensure_ascii=False) may emit.
            raw = json.dumps(data, indent=2, ensure_ascii=False)
            safe = raw.encode("utf-8", errors="replace").decode("utf-8")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(safe)
            tmp.replace(self._index_path)
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
            from .utils import sanitize_surrogates
            with open(session_file, "w", encoding="utf-8") as f:
                for msg in messages:
                    safe_msg = sanitize_surrogates(msg)
                    # Round-trip through ASCII-safe JSON to guarantee no lone
                    # surrogates reach f.write() — ensure_ascii=True escapes
                    # all non-ASCII, then we decode back to unicode cleanly.
                    line = json.dumps(safe_msg, ensure_ascii=False)
                    # Belt-and-suspenders: encode→decode strips any remaining
                    # lone surrogates that json.dumps may have emitted.
                    line = line.encode("utf-8", errors="replace").decode("utf-8")
                    f.write(line + "\n")
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

    def resolve_session_id(self, session_id: str) -> str | None:
        """
        Resolve a session ID, supporting partial hash matching.

        - Exact match → return as-is.
        - 8-char hex hash → find the session whose ID ends with that hash.
        - Multiple matches → return the most recently updated.
        Returns None if no match found.
        """
        if session_id in self._index:
            return session_id
        # Try hash-part match (user typed any of the three 8-char hashes)
        if len(session_id) >= 4 and all(c in "0123456789abcdef" for c in session_id):
            candidates = [
                (meta.updated, sid)
                for sid, meta in self._index.items()
                if session_id in sid  # matches any part of the 3-part ID
            ]
            if candidates:
                candidates.sort(reverse=True)
                best = candidates[0][1]
                logger.info("Resolved session %s → %s", session_id, best)
                return best
        return None

    def load(self, session_id: str) -> list[dict[str, Any]] | None:
        """
        Load a session's messages from disk.
        Supports partial hash matching via resolve_session_id().
        Returns list of message dicts or None if not found.
        """
        sid = self.resolve_session_id(session_id)
        if sid is None:
            logger.warning("Session not found: %s", session_id)
            return None
        session_file = self._base_dir / f"{sid}.jsonl"
        if not session_file.exists():
            logger.warning("Session file not found: %s", sid)
            return None

        # Safety: refuse to load files > 100 MB (prevent memory exhaustion)
        try:
            st = session_file.stat()
            if st.st_size > 100_000_000:
                logger.error("Session file too large: %s (%d bytes)", session_id, st.st_size)
                return None
        except OSError:
            pass

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
        # Resolve partial hash prefixes to full UUIDs (like load() does)
        resolved = self.resolve_session_id(session_id)
        if resolved:
            session_id = resolved
        session_file = self._base_dir / f"{session_id}.jsonl"
        deleted = False
        try:
            session_file.unlink(missing_ok=True)
            deleted = True
        except Exception as e:
            logger.error("Failed to delete session file %s: %s", session_id, e)
            # Still clean up index — don't leak stale entries

        if session_id in self._index:
            del self._index[session_id]
            self._save_index()
            deleted = True

        return deleted

    def get_meta(self, session_id: str) -> SessionMeta | None:
        """Get session metadata."""
        return self._index.get(session_id)

    def list_sessions(self, limit: int = 20,
                       workspace: str | None = None) -> list[SessionMeta]:
        """List sessions, newest first. Optionally filter by workspace."""
        sessions = self._index.values()
        if workspace:
            ws_normalized = str(Path(workspace).resolve())
            sessions = [
                s for s in sessions
                if s.workspace and str(Path(s.workspace).resolve()) == ws_normalized
            ]
        sorted_sessions = sorted(sessions, key=lambda m: m.updated, reverse=True)
        return sorted_sessions[:limit]

    def search_sessions(self, query: str,
                         workspace: str | None = None) -> list[SessionMeta]:
        """Search sessions by summary text. Optionally filter by workspace."""
        q = query.lower()
        results = []
        for meta in self._index.values():
            if q in meta.summary.lower() or q in meta.id.lower():
                results.append(meta)
        if workspace:
            ws_normalized = str(Path(workspace).resolve())
            results = [
                r for r in results
                if r.workspace and str(Path(r.workspace).resolve()) == ws_normalized
            ]
        return sorted(results, key=lambda m: m.updated, reverse=True)

    def get_recent(self, count: int = 5,
                    workspace: str | None = None) -> list[SessionMeta]:
        """Get the most recent sessions, optionally filtered to workspace."""
        return self.list_sessions(limit=count, workspace=workspace)

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
            "",
            f"- **Created:** {meta.created if meta else 'unknown'}",
            f"- **Model:** {meta.model if meta else 'unknown'}",
            f"- **Skill:** {meta.skill if meta else 'none'}",
            f"- **Messages:** {len(messages)}",
            "",
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
        from .utils import sanitize_surrogates
        data = sanitize_surrogates({
            "session_id": session_id,
            "metadata": meta.to_dict() if meta else {},
            "messages": messages,
        })
        json_str = json.dumps(data, indent=2, ensure_ascii=False)

        if output_path:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(json_str)

        return json_str


# ── Auto-save helper ─────────────────────────────────────────────────────────

def generate_session_id(task: str, skill: str = "", workspace: str = "") -> str:
    """
    Generate a 3-part hash-based session ID.

    Format: ``xxxxxxxx-xxxxxxxx-xxxxxxxx`` (8-8-8 hex)

    - Part 1: SHA256(workspace path) → first 8 chars — groups by project
    - Part 2: SHA256(ISO timestamp) → first 8 chars — unique per session
    - Part 3: SHA256(task text) → first 8 chars — identifies the task

    The ``workspace`` parameter hashes the current working directory,
    so ``//resume`` can find all sessions for a given project folder.
    """
    import hashlib
    # Sanitize inputs — lone surrogates crash .encode()
    from .utils import sanitize_surrogates
    ws_safe = sanitize_surrogates(workspace or "")
    task_safe = sanitize_surrogates(task or "conversation")
    # Part 1: workspace hash (project-scoped)
    ws_hash = hashlib.sha256(ws_safe.encode()).hexdigest()[:8]
    # Part 2: timestamp hash (unique per session)
    now_iso = datetime.now(timezone.utc).isoformat()
    time_hash = hashlib.sha256(now_iso.encode()).hexdigest()[:8]
    # Part 3: task title hash (identifies the conversation topic)
    task_hash = hashlib.sha256(task_safe.encode()).hexdigest()[:8]
    return f"{ws_hash}-{time_hash}-{task_hash}"
