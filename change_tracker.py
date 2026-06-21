"""
Change Tracker — undo/redo for all file modifications.

Every write_file and edit_file operation is tracked with:
- Before/after content snapshots
- Timestamp and tool call context
- File path and operation type

Supports:
- /undo <n>       — Revert the last N changes
- /undo all       — Revert all changes in this session
- /changes        — List all changes with diffs
- /restore <n>    — Re-apply a reverted change
- Auto-backup      — Files are backed up before modification

Backups stored in: .ata_coder/changes/<session-id>/
"""

import difflib
import logging
import shutil
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


def _default_changes_dir() -> Path:
    """Get the default changes backup directory from settings or fallback."""
    try:
        from .settings import get_settings
        return get_settings().changes_dir
    except Exception:
        return Path.home() / ".ata_coder" / "changes"


# ═══════════════════════════════════════════════════════════════════════════════
# Data model
# ═══════════════════════════════════════════════════════════════════════════════

class ChangeType(Enum):
    WRITE = "write"    # New file created
    EDIT = "edit"      # Existing file modified
    DELETE = "delete"  # File deleted (via shell)


@dataclass
class FileChange:
    """A single tracked file change."""
    id: int
    file_path: str
    change_type: ChangeType
    old_content: str | None      # None for new files (WRITE)
    new_content: str | None      # None for deleted files
    timestamp: str = ""
    reverted: bool = False

    @property
    def diff(self) -> str:
        """Generate a unified diff."""
        old = (self.old_content or "").splitlines(keepends=True)
        new = (self.new_content or "").splitlines(keepends=True)
        if not old and new:
            # New file — show all additions
            return "".join(f"+{line}" for line in new)
        if old and not new:
            # Deleted file — show all deletions
            return "".join(f"-{line}" for line in old)
        diff = difflib.unified_diff(
            old, new,
            fromfile=f"a/{self.file_path}" if self.old_content else "/dev/null",
            tofile=f"b/{self.file_path}" if self.new_content else "/dev/null",
        )
        return "".join(diff)

    @property
    def summary(self) -> str:
        """One-line summary."""
        status = "[REVERTED]" if self.reverted else ""
        if self.change_type == ChangeType.WRITE:
            return f"#{self.id} CREATE {self.file_path} {status}"
        elif self.change_type == ChangeType.EDIT:
            old_lines = (self.old_content or "").count("\n") + 1 if self.old_content else 0
            new_lines = (self.new_content or "").count("\n") + 1 if self.new_content else 0
            return f"#{self.id} EDIT   {self.file_path} ({old_lines}→{new_lines} lines) {status}"
        else:
            return f"#{self.id} DELETE {self.file_path} {status}"


# ═══════════════════════════════════════════════════════════════════════════════
# Change Tracker
# ═══════════════════════════════════════════════════════════════════════════════

class ChangeTracker:
    """
    Tracks all file modifications in a session for undo/redo.

    Also maintains auto-backups of files before modification.
    """

    def __init__(self, session_id: str = "", backup_dir: str | Path | None = None):
        self.session_id = session_id or time.strftime("%Y%m%d-%H%M%S")
        self.changes: list[FileChange] = []
        self._next_id = 1
        self._backup_dir = Path(backup_dir) if backup_dir else _default_changes_dir() / self.session_id
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        self._backups: dict[str, str] = {}
        self._dry_run = False
        self._last_active: int = -1
        self.workspace: Path | None = None  # set by agent for workspace boundary checks

    # ── Dry run toggle ───────────────────────────────────────────────────

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    @dry_run.setter
    def dry_run(self, enabled: bool):
        self._dry_run = enabled
        if enabled:
            logger.info("DRY RUN MODE enabled — no actual changes will be made")

    def reset(self) -> None:
        """Reset tracker state for a new agent run."""
        self.changes.clear()
        self._next_id = 1
        self._last_active = -1

    # ── Capture changes ──────────────────────────────────────────────────

    def capture_write(self, file_path: str, content: str) -> FileChange | None:
        """Track a file creation/write. The actual write is done by ToolExecutor."""
        path = Path(file_path)
        exists_before = path.exists()

        # Backup existing file (for undo)
        old_content = None
        if exists_before:
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    old_content = f.read()
                self._backup(file_path)
            except Exception:
                logger.debug("Failed to read/backup existing file %s", file_path, exc_info=True)

        change = FileChange(
            id=self._next_id,
            file_path=file_path,
            change_type=ChangeType.WRITE if not exists_before else ChangeType.EDIT,
            old_content=old_content,
            new_content=content,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

        if self._dry_run:
            # Dry-run: only save to backup dir, do NOT write to actual file.
            # Keep the original file_path so undo/restore target the right file
            # if dry-run is toggled off later.
            dry_path = self._backup_dir / f"dry_{self._next_id}_{Path(file_path).name}"
            dry_path.parent.mkdir(parents=True, exist_ok=True)
            with open(dry_path, "w", encoding="utf-8", errors="replace") as f:
                f.write(content)
            logger.info("[DRY-RUN] Would write: %s → %s (backup: %s)", file_path, file_path, dry_path)
        # Normal mode: actual file write is done by ToolExecutor, we just track + backup

        self.changes.append(change)
        self._next_id += 1
        self._last_active = -1  # reset cursor after new change
        return change

    def capture_edit(self, file_path: str, old_content: str, new_content: str) -> FileChange | None:
        """Track a file edit. The actual edit is done by ToolExecutor."""
        if old_content == new_content:
            return None

        # Backup before edit (for undo)
        self._backup(file_path)

        change = FileChange(
            id=self._next_id,
            file_path=file_path,
            change_type=ChangeType.EDIT,
            old_content=old_content,
            new_content=new_content,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

        if self._dry_run:
            # Dry-run: only save to backup dir, do NOT write to actual file.
            # Keep the original file_path so undo/restore target the right file
            # if dry-run is toggled off later (consistent with capture_write).
            dry_path = self._backup_dir / f"dry_{self._next_id}_{Path(file_path).name}"
            dry_path.parent.mkdir(parents=True, exist_ok=True)
            with open(dry_path, "w", encoding="utf-8", errors="replace") as f:
                f.write(new_content)
            logger.info("[DRY-RUN] Would edit: %s → %s (backup: %s)", file_path, file_path, dry_path)
        # Normal mode: actual file edit is done by ToolExecutor, we just track + backup

        self.changes.append(change)
        self._next_id += 1
        self._last_active = -1  # reset cursor after new change
        return change

    # ── Backup ───────────────────────────────────────────────────────────

    def _backup(self, file_path: str) -> str:
        """Create a timestamped backup of a file."""
        path = Path(file_path)
        if not path.exists():
            return ""

        # Use nanosecond precision to avoid backup collisions when two
        # operations touch the same file within the same second.
        backup_name = f"{path.name}.{time.time_ns()}.bak"
        backup_path = self._backup_dir / backup_name
        shutil.copy2(str(path), str(backup_path))
        self._backups[file_path] = str(backup_path)
        logger.debug("Backed up: %s → %s", file_path, backup_path.name)
        return str(backup_path)

    # ── Undo ─────────────────────────────────────────────────────────────

    def undo(self, count: int = 1) -> list[FileChange]:
        """Undo the last N changes. O(n) using _last_active index."""
        if self._dry_run:
            return []

        reverted = []
        for _ in range(count):
            if self._last_active < 0:
                self._last_active = len(self.changes) - 1
            while self._last_active >= 0 and self.changes[self._last_active].reverted:
                self._last_active -= 1
            if self._last_active < 0:
                break

            c = self.changes[self._last_active]
            self._apply_revert(c)
            c.reverted = True
            reverted.append(c)
            self._last_active -= 1
            logger.info("Undid change #%d: %s", c.id, c.file_path)

        return reverted

    def _apply_revert(self, c: FileChange) -> None:
        """Apply revert for a single change."""
        path = Path(c.file_path)
        # Safety: skip paths outside the workspace (defense in depth)
        if self.workspace is not None:
            try:
                path.resolve().relative_to(self.workspace.resolve())
            except ValueError:
                logger.warning("Skipping undo outside workspace: %s", c.file_path)
                return
        if c.change_type == ChangeType.WRITE:
            if c.old_content is None:
                if path.exists():
                    path.unlink()
            elif path.exists():
                path.write_text(c.old_content, encoding="utf-8", errors="replace")
        elif c.change_type == ChangeType.EDIT:
            if path.exists() and c.old_content is not None:
                path.write_text(c.old_content, encoding="utf-8", errors="replace")
        elif c.change_type == ChangeType.DELETE:
            if c.new_content is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(c.new_content, encoding="utf-8", errors="replace")

    def undo_all(self) -> list[FileChange]:
        """Undo all changes in this session."""
        active = sum(1 for c in self.changes if not c.reverted)
        return self.undo(active)

    def restore(self, change_id: int) -> FileChange | None:
        """Re-apply a previously reverted change."""
        for c in self.changes:
            if c.id == change_id and c.reverted:
                path = Path(c.file_path)
                if c.new_content is not None:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with open(path, "w", encoding="utf-8", errors="replace") as f:
                        f.write(c.new_content)
                c.reverted = False
                logger.info("Restored change #%d: %s", c.id, c.file_path)
                return c
        return None

    # ── List & summary ───────────────────────────────────────────────────

    def list_changes(self, include_reverted: bool = False) -> list[FileChange]:
        """List all changes."""
        if include_reverted:
            return list(self.changes)
        return [c for c in self.changes if not c.reverted]

    def summary(self) -> str:
        """Multi-line summary of all changes."""
        active = self.list_changes()
        reverted = [c for c in self.changes if c.reverted]

        lines = []
        lines.append(f"Session: {self.session_id}")
        lines.append(f"Changes: {len(active)} active, {len(reverted)} reverted")
        lines.append(f"Dry-run: {'ON' if self._dry_run else 'OFF'}")
        lines.append("")

        if active:
            for c in active:
                lines.append(f"  {c.summary}")
        else:
            lines.append("  (no active changes)")

        if reverted:
            lines.append("\n  Reverted:")
            for c in reverted:
                lines.append(f"  {c.summary}")

        return "\n".join(lines)

    def diff_summary(self, last_n: int = 5) -> str:
        """Show diffs for the last N changes."""
        recent = [c for c in self.changes if not c.reverted][-last_n:]
        if not recent:
            return "(no changes to show)"

        parts = []
        for c in recent:
            parts.append(f"--- {c.summary} ---")
            parts.append(c.diff[:2000])
            parts.append("")
        return "\n".join(parts)

    def count_active(self) -> int:
        return sum(1 for c in self.changes if not c.reverted)

    def count_all(self) -> int:
        return len(self.changes)


# ═══════════════════════════════════════════════════════════════════════════════
# Session change tracker (global, manages multiple sessions)
# ═══════════════════════════════════════════════════════════════════════════════

class SessionChangeManager:
    """Manages change trackers across multiple sessions."""

    def __init__(self, base_dir: str | Path | None = None):
        self._base = Path(base_dir) if base_dir else _default_changes_dir()
        self._base.mkdir(parents=True, exist_ok=True)
        self._trackers: dict[str, ChangeTracker] = {}

    def get(self, session_id: str) -> ChangeTracker:
        if session_id not in self._trackers:
            backup_dir = self._base / session_id
            self._trackers[session_id] = ChangeTracker(session_id, backup_dir)
        return self._trackers[session_id]

    def list_sessions(self) -> list[str]:
        """List sessions that have changes."""
        sessions = []
        if self._base.exists():
            for d in self._base.iterdir():
                if d.is_dir():
                    sessions.append(d.name)
        return sorted(sessions, reverse=True)

    def cleanup_session(self, session_id: str) -> bool:
        """Remove change tracker and its backups."""
        if session_id in self._trackers:
            del self._trackers[session_id]
        backup_dir = self._base / session_id
        if backup_dir.exists():
            shutil.rmtree(str(backup_dir))
            return True
        return False
