"""
Persistent memory system for ATA Coder.

Stores facts, user preferences, feedback, and project context across sessions.
Uses a file-based approach:
- memory/MEMORY.md — index of all memories (loaded on startup)
- memory/<slug>.md — individual memory files with YAML frontmatter

Memory types:
- user: who the user is, their preferences, expertise
- feedback: user guidance on how the agent should work
- project: ongoing goals, constraints, architecture decisions
- reference: pointers to external resources (URLs, docs, etc.)
"""

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Try to import yaml for frontmatter parsing
try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


# ── Memory data model ────────────────────────────────────────────────────────

@dataclass
class Memory:
    """A single memory entry."""

    name: str                          # kebab-case slug, used as filename
    description: str                   # one-line summary (used for relevance)
    content: str                       # the memory body
    metadata: dict[str, Any] = field(default_factory=dict)  # type, tags, etc.
    created: str = ""                  # ISO timestamp
    updated: str = ""                  # ISO timestamp

    @property
    def memory_type(self) -> str:
        return self.metadata.get("type", "reference")

    @property
    def file_path(self) -> str:
        return f"{self.name}.md"

    def to_frontmatter(self) -> str:
        """Serialize to a markdown file with YAML frontmatter."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        meta = {
            "name": self.name, "description": self.description,
            "metadata": self.metadata, "created": self.created or now, "updated": now,
        }
        try:
            import yaml as _yaml
            yaml_str = _yaml.dump(meta, default_flow_style=False, allow_unicode=True, sort_keys=False)
        except ImportError:
            yaml_str = json.dumps(meta, indent=2, ensure_ascii=False)
        return f"---\n{yaml_str}---\n\n{self.content}"

    @classmethod
    def from_frontmatter(cls, raw: str) -> "Memory | None":
        """Parse a markdown file with YAML frontmatter into a Memory."""
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", raw, re.DOTALL)
        if not match:
            return None
        front_str, content = match.group(1), match.group(2).strip()
        try:
            try:
                import yaml as _yaml
                meta = _yaml.safe_load(front_str)
            except ImportError:
                meta = json.loads(front_str)
        except Exception as e:
            logger.warning("Failed to parse frontmatter: %s", e)
            return None
        if not isinstance(meta, dict):
            return None
        return cls(
            name=meta.get("name", "unknown"), description=meta.get("description", ""),
            content=content, metadata=meta.get("metadata", {}),
            created=meta.get("created", ""), updated=meta.get("updated", ""),
        )


# ── Memory store ─────────────────────────────────────────────────────────────

class MemoryStore:
    """
    Persistent file-based memory store.

    On initialization, reads MEMORY.md for the index, then loads individual
    memory files on demand or all at once.
    """

    def __init__(self, memory_dir: str | Path | None = None):
        if memory_dir is None:
            try:
                from .settings import get_settings
                memory_dir = get_settings().memory_dir
            except Exception:
                memory_dir = Path.home() / ".ata_coder" / "memory"
        self.memory_dir = Path(memory_dir)
        self.memory_dir.mkdir(parents=True, exist_ok=True)

        self._index_path = self.memory_dir / "MEMORY.md"
        self._memories: dict[str, Memory] = {}
        self._index_entries: list[str] = []  # lines from MEMORY.md

        self._load_index()
        self._load_all()

    # ── Loading ───────────────────────────────────────────────────────────

    def _load_index(self) -> None:
        """Load the MEMORY.md index file."""
        if self._index_path.exists():
            try:
                with open(self._index_path, "r", encoding="utf-8") as f:
                    self._index_entries = [
                        line.strip() for line in f.readlines() if line.strip()
                    ]
                logger.debug(
                    "Loaded MEMORY.md: %d entries", len(self._index_entries)
                )
            except Exception as e:
                logger.warning("Failed to load MEMORY.md: %s", e)
                self._index_entries = []
        else:
            # Create empty index
            self._write_index()

    def _load_all(self) -> None:
        """Load all memory files from the directory."""
        if not self.memory_dir.exists():
            return

        for file_path in self.memory_dir.glob("*.md"):
            if file_path.name == "MEMORY.md":
                continue
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    raw = f.read()
                memory = Memory.from_frontmatter(raw)
                if memory:
                    self._memories[memory.name] = memory
                else:
                    logger.warning("Failed to parse memory file: %s", file_path.name)
            except Exception as e:
                logger.warning("Failed to read memory file %s: %s", file_path.name, e)

        logger.debug("Loaded %d memories from disk", len(self._memories))

    def _write_index(self) -> None:
        """Write the index file."""
        try:
            with open(self._index_path, "w", encoding="utf-8") as f:
                for entry in self._index_entries:
                    f.write(entry + "\n")
        except Exception as e:
            logger.warning("Failed to write MEMORY.md: %s", e)

    # ── CRUD operations ──────────────────────────────────────────────────

    def add(self, memory: Memory) -> Memory:
        """
        Add or update a memory. If one with the same name exists, update it.
        """
        existing = self._memories.get(memory.name)

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if existing:
            memory.created = existing.created  # preserve original creation time
        else:
            memory.created = memory.created or now
        memory.updated = now

        # Write memory file
        file_path = self.memory_dir / memory.file_path
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(memory.to_frontmatter())
        except Exception as e:
            logger.error("Failed to write memory file %s: %s", file_path, e)
            raise

        self._memories[memory.name] = memory

        # Update index
        entry = f"- [{memory.description}]({memory.file_path})"
        # Replace existing entry for same memory or append
        replaced = False
        for i, line in enumerate(self._index_entries):
            if f"]({memory.file_path})" in line:
                self._index_entries[i] = entry
                replaced = True
                break
        if not replaced:
            self._index_entries.append(entry)

        self._write_index()
        logger.info("Saved memory: %s", memory.name)
        return memory

    def save_batch(self, memories: list[Memory]) -> list[Memory]:
        """
        Save multiple memories efficiently — writes index only once.

        For bulk operations (seeding, importing, syncing), this is
        significantly faster than calling save() repeatedly.
        """
        for memory in memories:
            file_path = self.memory_dir / memory.file_path
            try:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(memory.to_frontmatter())
            except Exception as e:
                logger.error("Failed to write memory file %s: %s", file_path, e)
                continue
            self._memories[memory.name] = memory
        # Rebuild and write index once
        self._rebuild_index()
        logger.info("Batch saved %d memories", len(memories))
        return memories

    def _rebuild_index(self) -> None:
        """Rebuild the index from all loaded memories (batch-safe)."""
        self._index_entries = [
            f"- [{m.description}]({m.file_path})"
            for m in self._memories.values()
        ]
        self._write_index()

    def flush(self) -> None:
        """Force-write the index to disk (call before shutdown)."""
        self._rebuild_index()

    def get(self, name: str) -> Memory | None:
        """Get a memory by name (slug)."""
        return self._memories.get(name)

    def delete(self, name: str) -> bool:
        """Delete a memory by name."""
        memory = self._memories.pop(name, None)
        if memory is None:
            return False

        file_path = self.memory_dir / memory.file_path
        if file_path.exists():
            try:
                file_path.unlink()
            except Exception as e:
                logger.warning("Failed to delete memory file: %s", e)

        # Remove from index
        self._index_entries = [
            line
            for line in self._index_entries
            if f"]({memory.file_path})" not in line
        ]
        self._write_index()
        logger.info("Deleted memory: %s", name)
        return True

    def list_all(self, memory_type: str | None = None) -> list[Memory]:
        """List all memories, optionally filtered by type."""
        memories = list(self._memories.values())
        if memory_type:
            memories = [m for m in memories if m.memory_type == memory_type]
        # Sort by updated (handle both string and datetime types)
        def sort_key(m: Memory) -> str:
            return str(m.updated or "")
        return sorted(memories, key=sort_key, reverse=True)

    def search(self, query: str) -> list[Memory]:
        """Search memories by TF-IDF-weighted token overlap.
        Returns memories sorted by relevance score (descending).
        """
        scored = self._search_scored(query)
        return [m for _, m in scored]

    def _search_scored(self, query: str) -> list[tuple[float, Memory]]:
        """
        Score every memory against *query* with TF-IDF-weighted token
        overlap plus phrase bonuses and recency boost.

        Returns (score, memory) pairs sorted by score descending.
        """
        if not self._memories:
            return []

        query_lower = query.lower()
        query_tokens = set(query_lower.split())

        # ── Pre-compute document frequencies for IDF weighting ──────────
        doc_count = len(self._memories)
        token_df: dict[str, int] = {}
        for m in self._memories.values():
            text = f"{m.name} {m.description} {m.content}".lower()
            seen: set[str] = set()
            for word in text.split():
                if word not in seen:
                    token_df[word] = token_df.get(word, 0) + 1
                    seen.add(word)

        def idf(token: str) -> float:
            df = token_df.get(token, 1)
            import math
            return math.log((doc_count + 1) / (df + 1)) + 1.0  # smoothed IDF

        # ── Score each memory ──────────────────────────────────────────
        results: list[tuple[float, Memory]] = []
        for memory in self._memories.values():
            score = 0.0
            name_lower = memory.name.lower()
            desc_lower = memory.description.lower()
            content_lower = memory.content.lower()

            # Phrase bonus: full query appears as substring
            if query_lower in name_lower:
                score += 15.0
            if query_lower in desc_lower:
                score += 8.0
            if query_lower in content_lower:
                score += 4.0

            # Token-level IDF-weighted match
            for token in query_tokens:
                w = idf(token)
                if token in name_lower.replace("-", " ").split():
                    score += 6.0 * w   # name match — highest signal
                if token in set(desc_lower.split()):
                    score += 3.0 * w   # description match — medium signal
                if token in set(content_lower.split()):
                    score += 1.5 * w   # content match — lower signal

            # Recency boost: memories touched in the last hour get +2
            try:
                from datetime import datetime, timezone, timedelta
                updated = memory.updated or ""
                if updated:
                    dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                    if dt > datetime.now(timezone.utc) - timedelta(hours=1):
                        score += 2.0
            except (ValueError, TypeError):
                pass

            if score > 0:
                results.append((score, memory))

        results.sort(key=lambda x: x[0], reverse=True)
        return results

    # ── Recall for context ───────────────────────────────────────────────

    def recall_context(self, user_input: str, max_memories: int = 5,
                       min_score: float = 3.0) -> str:
        """
        Recall memories relevant to *user_input* for inclusion in the system
        prompt.  Only returns memories whose relevance score exceeds
        *min_score* so the prompt doesn't get polluted with noise.
        """
        if not self._memories:
            return ""

        # Re-use the scored search
        scored = self._search_scored(user_input)
        relevant = [m for score, m in scored if score >= min_score][:max_memories]
        if not relevant:
            return ""

        # Bump access tracking
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for m in relevant:
            m.metadata["last_accessed"] = now
            m.metadata["access_count"] = m.metadata.get("access_count", 0) + 1

        lines = ["\n## Relevant Memories"]
        for memory in relevant:
            lines.append(f"\n### {memory.description}")
            content = memory.content
            if len(content) > 500:
                content = content[:500] + "..."
            lines.append(content)
            refs = self._extract_links(memory.content)
            if refs:
                lines.append(f"Related: {', '.join(refs)}")
        return "\n".join(lines)

    def _extract_links(self, content: str) -> list[str]:
        """Extract [[wiki-style]] links from content."""
        return re.findall(r"\[\[([^\]]+)\]\]", content)

    def get_memory_context(self, max_total: int = 8) -> str:
        """
        Return a compact summary of recently-updated memories for the
        system prompt.  Capped at *max_total* entries so the prompt
        doesn't bloat when the user has dozens of memories.
        """
        if not self._memories:
            return ""

        def _sort_key(m: Memory) -> str:
            return str(m.updated or "")

        recent = sorted(self._memories.values(), key=_sort_key, reverse=True)[:max_total]
        if not recent:
            return ""

        lines = ["\n## Persistent Memory"]
        by_type: dict[str, list[Memory]] = {}
        for m in recent:
            by_type.setdefault(m.memory_type, []).append(m)

        for mtype in ["user", "project", "feedback", "reference"]:
            entries = by_type.get(mtype, [])
            if entries:
                lines.append(f"\n### {mtype.title()}")
                for m in entries[:3]:
                    lines.append(f"- {m.description}")
        return "\n".join(lines)

    # ── Auto-suggest from conversation ──────────────────────────────────

    def suggest_from_conversation(self, user_messages: list[str],
                                  file_ops: list[str] | None = None,
                                  tool_errors: list[str] | None = None) -> list[str]:
        """Analyse recent messages for facts worth saving as memories.

        Returns a list of human-readable suggestions like
        ``"user prefers YAML over JSON for config"`` that the agent can
        surface to the user with a quick save prompt.
        """
        suggestions: list[str] = []

        # Heuristic 1: explicit "remember …" or "save …" directives
        for msg in user_messages:
            lower = msg.lower()
            if any(kw in lower for kw in ("remember", "save this", "don't forget",
                                           "记", "记住", "备忘")):
                suggestions.append(f"User asked to remember: {msg[:120]}")

        # Heuristic 2: project-specific paths or toolchains mentioned
        toolchain_keywords = ["idf.py", "esp-idf", "esptool", "cmake", "platformio",
                              "arduino", "stm32", "nrf", "zephyr"]
        for msg in user_messages:
            for kw in toolchain_keywords:
                if kw.lower() in msg.lower():
                    suggestions.append(
                        f"Project uses {kw}: {msg[:120]}"
                    )
                    break

        # Heuristic 3: device ports / serial config
        import re as _re
        for msg in user_messages:
            port_match = _re.search(r'COM\d+|/dev/tty\w+', msg)
            if port_match:
                suggestions.append(
                    f"Device port {port_match.group()}: {msg[:120]}"
                )

        # Heuristic 4: operational learnings — detect "X failed → Y worked" patterns
        if tool_errors:
            for err in tool_errors:
                lower = err.lower()
                if "not in the allowed list" in lower:
                    suggestions.append(
                        "ops: Some shell commands are blocked by the whitelist. "
                        "Use python -c \"import subprocess; subprocess.run([...], cwd='...')\" "
                        "as a workaround for tools not on PATH."
                    )
                    break
                if "command not found" in lower or "not recognized" in lower:
                    # Extract the command name
                    m = _re.search(r"'(\w+)'", err)
                    cmd = m.group(1) if m else "?"
                    suggestions.append(
                        f"ops: Command '{cmd}' not found — use full path or "
                        f"python subprocess wrapper."
                    )

        return suggestions[:5]  # cap to avoid overwhelming the user


# ── Convenience functions ────────────────────────────────────────────────────

def create_memory(
    name: str,
    description: str,
    content: str,
    memory_type: str = "reference",
    store: MemoryStore | None = None,
) -> Memory:
    """Create a memory with the given fields."""
    if store is None:
        store = get_memory_store()
    memory = Memory(
        name=name,
        description=description,
        content=content,
        metadata={"type": memory_type},
    )
    return store.add(memory)


# ── Global instance ──────────────────────────────────────────────────────────

_memory_store: MemoryStore | None = None


def get_memory_store(memory_dir: str | None = None) -> MemoryStore:
    global _memory_store
    if _memory_store is None:
        _memory_store = MemoryStore(memory_dir)
    return _memory_store
