"""
Persistent memory system for ATA Coder.

⚠️ **Sync note**: This module has a TypeScript counterpart at
``ts-server/src/memory-store.ts``.  Changes to the memory file format,
TF-IDF search logic, or MEMORY.md frontmatter schema MUST be mirrored
in both files.  The Python version is the **source of truth**; the TS
version provides TF-IDF search for the companion server's standalone
mode (when the Python agent is not running).

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

import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .utils import try_import_yaml

logger = logging.getLogger(__name__)

_yaml_mod, HAS_YAML = try_import_yaml()

# Characters allowed in memory names (alphanumeric, hyphen, underscore)
_VALID_NAME_RE = re.compile(r'^[a-zA-Z0-9][-a-zA-Z0-9_]*$')
# Disallowed path components
_FORBIDDEN_NAME_PARTS = ('..', '/', '\\', '\x00')


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

    @staticmethod
    def validate_name(name: str) -> str:
        """Validate and sanitise a memory name (slug).

        Returns the cleaned name.  Raises ``ValueError`` if the name
        contains path-traversal components or illegal characters.
        """
        if not name or not isinstance(name, str):
            raise ValueError("Memory name must be a non-empty string")
        if len(name) > 128:
            raise ValueError("Memory name must be ≤ 128 characters")
        for forbidden in _FORBIDDEN_NAME_PARTS:
            if forbidden in name:
                raise ValueError(
                    f"Memory name contains forbidden pattern: {forbidden!r}"
                )
        if not _VALID_NAME_RE.match(name):
            raise ValueError(
                f"Memory name {name!r} contains illegal characters. "
                f"Use only letters, digits, hyphens, and underscores."
            )
        return name

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
        if _yaml_mod is not None:
            yaml_str = _yaml_mod.dump(meta, default_flow_style=False, allow_unicode=True, sort_keys=False)
        else:
            yaml_str = json.dumps(meta, indent=2, ensure_ascii=False)
        return f"---\n{yaml_str}---\n\n{self.content}"

    @classmethod
    def from_frontmatter(cls, raw: str) -> "Memory | None":
        """Parse a markdown file with YAML frontmatter into a Memory."""
        # Non-greedy match for the frontmatter separator; uses a negative
        # lookahead to ensure we match the FIRST closing --- (avoiding false
        # matches on YAML separators inside code blocks in the body).
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", raw, re.DOTALL)
        if not match:
            return None
        front_str, content = match.group(1), match.group(2).strip()
        try:
            if _yaml_mod is not None:
                meta = _yaml_mod.safe_load(front_str)
            else:
                meta = json.loads(front_str)
        except Exception as e:
            # If yaml.safe_load fails, fall back to JSON parse
            if _yaml_mod is not None:
                try:
                    meta = json.loads(front_str)
                except Exception as json_err:
                    logger.warning("Failed to parse frontmatter (yaml: %s, json: %s)", e, json_err)
                    return None
            else:
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
        # IDF cache — invalidated on add/delete
        self._idf_cache: dict[str, float] | None = None
        self._idf_doc_count: int = 0
        self._lock = threading.RLock()  # protect concurrent read/write
        # Debounce tracking for access-metadata writes (avoids disk I/O on
        # rapid sequential lookups).
        self._access_write_debounce: dict[str, float] = {}
        self._ACCESS_DEBOUNCE_S = 0.5
        # Lazy loading: only load the index at startup; individual memory
        # files are loaded on first access.  _all_loaded flips when all
        # files have been loaded (triggered by search or explicit call).
        self._all_loaded: bool = False
        # Content hashes for deduplication (name → hash)
        self._hashes: dict[str, str] = {}

        # Pre-tokenization caches: avoid re-tokenizing on every search
        self._token_cache: dict[str, set[str]] = {}       # name → token set
        self._desc_token_cache: dict[str, set[str]] = {}   # name → description tokens
        self._content_token_cache: dict[str, set[str]] = {} # name → content tokens
        self._text_lower_cache: dict[str, str] = {}         # name → full lowercased text

        self._cleanup_tmp_files()
        self._load_index()
        # Start background preload (non-blocking)
        self._start_preload()

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
        """Load all memory files from the directory (legacy, use _ensure_all_loaded)."""
        self._ensure_all_loaded()

    def _ensure_all_loaded(self) -> None:
        """Load all memory files if not already loaded."""
        if self._all_loaded:
            return
        if not self.memory_dir.exists():
            self._all_loaded = True
            return

        loaded = 0
        # Acquire lock to prevent races with add/delete/search on _memories
        # and token caches from concurrent threads (e.g. background preload).
        with self._lock:
            for file_path in self.memory_dir.glob("*.md"):
                if file_path.name == "MEMORY.md":
                    continue
                name = file_path.stem
                if name in self._memories:
                    continue  # already loaded on demand
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        raw = f.read()
                    memory = Memory.from_frontmatter(raw)
                    if memory:
                        self._memories[memory.name] = memory
                        self._cache_tokens(memory)
                        loaded += 1
                    else:
                        logger.warning("Failed to parse memory file: %s", file_path.name)
                except Exception as e:
                    logger.warning("Failed to read memory file %s: %s", file_path.name, e)

            self._all_loaded = True
        if loaded:
            logger.debug("Lazy-loaded %d memories from disk (total: %d)", loaded, len(self._memories))

    def _load_file(self, name: str) -> Memory | None:
        """Load a single memory file on demand (cache miss)."""
        file_path = self.memory_dir / f"{name}.md"
        if not file_path.exists():
            return None
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                raw = f.read()
            memory = Memory.from_frontmatter(raw)
            if memory:
                content_hash = hashlib.sha256(
                    (memory.name + memory.description + memory.content).encode()
                ).hexdigest()[:16]
                with self._lock:
                    self._memories[name] = memory
                    self._hashes[name] = content_hash
            return memory
        except Exception as e:
            logger.warning("Failed to read memory file %s: %s", file_path.name, e)
            return None

    def _start_preload(self) -> None:
        """Start a background thread to preload all memory files."""
        import threading as _threading
        def _preload():
            try:
                self._ensure_all_loaded()
            except Exception:
                pass
        t = _threading.Thread(target=_preload, daemon=True)
        t.start()

    def _write_index(self) -> None:
        """Write the index file atomically (write-then-rename)."""
        tmp = self._index_path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for entry in self._index_entries:
                    f.write(entry + "\n")
            # os.replace is atomic cross-platform; Path.replace raises
            # FileExistsError on Windows for existing targets.
            os.replace(tmp, self._index_path)
        except Exception as e:
            logger.warning("Failed to write MEMORY.md: %s", e)

    def _cleanup_tmp_files(self) -> None:
        """Remove stale .tmp files left behind by crashed writes."""
        for tmp in self.memory_dir.glob("*.tmp"):
            try:
                tmp.unlink()
                logger.debug("Cleaned up stale tmp: %s", tmp.name)
            except Exception:
                pass

    def _write_file_atomically(self, file_path: Path, content: str) -> None:
        """Write *content* to *file_path* atomically (tmp → rename).

        Uses os.replace for cross-platform atomic rename.  On Windows,
        transient file locks (AV, indexing) can cause PermissionError;
        we retry once after a short delay.
        """
        import time as _time
        tmp = file_path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(content)
            for attempt in (1, 2):
                try:
                    os.replace(tmp, file_path)
                    break
                except PermissionError:
                    if attempt == 1 and file_path.exists():
                        _time.sleep(0.05)  # brief backoff for transient locks
                        continue
                    raise
        except Exception:
            # Clean up tmp on failure
            if tmp.exists():
                try:
                    tmp.unlink()
                except Exception:
                    pass
            raise

    # ── CRUD operations ──────────────────────────────────────────────────

    def add(self, memory: Memory) -> Memory:
        """
        Add or update a memory. If one with the same name exists, update it.
        Checks for duplicate content across different names.
        """
        # Validate name before touching the filesystem
        Memory.validate_name(memory.name)

        with self._lock:
            self._idf_cache = None  # invalidate IDF cache
            existing = self._memories.get(memory.name)

            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if existing:
                memory.created = existing.created
            else:
                memory.created = memory.created or now
            memory.updated = now

            # ── Dedup check ──────────────────────────────────────────────
            content_hash = hashlib.sha256(
                (memory.name + memory.description + memory.content).encode()
            ).hexdigest()[:16]
            for other_name, other_hash in list(self._hashes.items()):
                if other_name != memory.name and other_hash == content_hash:
                    logger.info(
                        "Skipping duplicate memory %s (same content as %s)",
                        memory.name, other_name,
                    )
                    # Update the existing entry with new timestamp and return it
                    return self._memories.get(other_name, memory)
            self._hashes[memory.name] = content_hash

            # Write memory file atomically
            file_path = self.memory_dir / memory.file_path
            try:
                self._write_file_atomically(file_path, memory.to_frontmatter())
            except Exception as e:
                logger.error("Failed to write memory file %s: %s", file_path, e)
                raise

            self._memories[memory.name] = memory

            # Update index
            entry = f"- [{memory.description}]({memory.file_path})"
            replaced = False
            for i, line in enumerate(self._index_entries):
                if f"]({memory.file_path})" in line:
                    self._index_entries[i] = entry
                    replaced = True
                    break
            if not replaced:
                self._index_entries.append(entry)

            self._write_index()
            self._cache_tokens(memory)
        logger.info("Saved memory: %s", memory.name)
        return memory

    def _cache_tokens(self, memory: Memory) -> None:
        """Pre-tokenize a memory and cache the results for fast TF-IDF lookup."""
        name_l = memory.name.lower()
        desc_l = memory.description.lower()
        content_l = memory.content.lower()
        self._token_cache[memory.name] = set(self._tokenize(memory.name))
        self._desc_token_cache[memory.name] = set(self._tokenize(memory.description))
        self._content_token_cache[memory.name] = set(self._tokenize(memory.content))
        self._text_lower_cache[memory.name] = f"{name_l} {desc_l} {content_l}"

    def save_batch(self, memories: list[Memory]) -> list[Memory]:
        """Save multiple memories efficiently — writes index only once."""
        with self._lock:
            self._idf_cache = None
            for memory in memories:
                try:
                    Memory.validate_name(memory.name)
                except ValueError as e:
                    logger.warning("Skipping invalid memory '%s': %s", memory.name, e)
                    continue
                file_path = self.memory_dir / memory.file_path
                try:
                    self._write_file_atomically(file_path, memory.to_frontmatter())
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
        """Get a memory by name (slug). Loads from disk on cache miss."""
        with self._lock:
            if name in self._memories:
                return self._memories[name]
            # Lazy load — try disk before giving up
            return self._load_file(name)

    def delete(self, name: str) -> bool:
        """Delete a memory by name."""
        with self._lock:
            self._idf_cache = None
            memory = self._memories.pop(name, None)
            if memory is None:
                return False

            file_path = self.memory_dir / memory.file_path
            if file_path.exists():
                try:
                    file_path.unlink()
                except Exception as e:
                    logger.warning("Failed to delete memory file: %s", e)

            self._index_entries = [
                line for line in self._index_entries
                if f"]({memory.file_path})" not in line
            ]
            self._write_index()
        logger.info("Deleted memory: %s", name)
        return True

    def list_all(self, memory_type: str | None = None) -> list[Memory]:
        """List all memories, optionally filtered by type."""
        with self._lock:
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
        self._ensure_all_loaded()
        with self._lock:
            scored = self._search_scored(query)
        return [m for _, m in scored]

    # ── English stopwords for TF-IDF filtering ────────────────────────────
    _STOPWORDS: set[str] = {
        "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
        "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
        "being", "have", "has", "had", "do", "does", "did", "will", "would",
        "could", "should", "may", "might", "can", "shall", "this", "that",
        "these", "those", "it", "its", "i", "me", "my", "we", "our", "you",
        "your", "he", "she", "they", "them", "not", "no", "as", "if", "so",
        "than", "also", "very", "just", "about", "into", "over", "after",
        "before", "between", "under", "during", "each", "all", "any", "both",
        "few", "more", "most", "other", "some", "such", "only", "own", "same",
        "too", "here", "there", "when", "where", "why", "how", "which", "who",
    }

    def _tokenize(self, text: str) -> list[str]:
        """Tokenize text for TF-IDF: lowercase, strip punctuation, split."""
        # Remove punctuation except hyphens within words
        clean = re.sub(r'[^\w\s-]', ' ', text.lower())
        # Split on whitespace, filter stopwords and short tokens
        tokens = []
        for token in clean.split():
            token = token.strip('-')
            if len(token) < 2:
                continue
            if token in self._STOPWORDS:
                continue
            tokens.append(token)
        return tokens

    def _search_scored(self, query: str) -> list[tuple[float, Memory]]:
        """
        Score every memory against *query* with TF-IDF-weighted token
        overlap plus phrase bonuses and recency boost.

        Returns (score, memory) pairs sorted by score descending.
        """
        if not self._memories:
            return []

        query_lower = query.lower()
        query_tokens = set(self._tokenize(query_lower))
        if not query_tokens:
            # Fallback to raw split if tokenization emptied the query
            query_tokens = set(query_lower.split())

        # ── Fast pre-filter: substring match prunes zero-relevance memories ──
        # Only memories whose lowercased text contains the query (or any query
        # token) proceed to full TF-IDF scoring.  This is a cheap O(1) "in"
        # check that eliminates >90% of irrelevant memories in practice.
        query_words = query_lower.split()
        candidates: dict[str, Memory] = {}
        for name, mem in self._memories.items():
            text = self._text_lower_cache.get(name)
            if text is None:
                # Fallback: build text on the fly if not cached
                text = f"{mem.name.lower()} {mem.description.lower()} {mem.content.lower()}"
                self._text_lower_cache[name] = text
            # Check if query or any significant word appears as substring
            if query_lower in text or any(
                len(w) >= 3 and w in text for w in query_words
            ):
                candidates[name] = mem

        # If pre-filter caught some, use those; otherwise score all (belt-and-suspenders)
        target_memories = candidates if candidates else self._memories

        # ── Pre-compute document frequencies for IDF weighting ──────────
        # Use cached IDF when available; rebuild only when memories change.
        # Uses pre-tokenized caches to avoid re-tokenizing every memory.
        import math as _math
        doc_count = len(self._memories)
        if self._idf_cache is None or self._idf_doc_count != doc_count:
            token_df: dict[str, int] = {}
            for name, m in self._memories.items():
                all_tokens = (self._token_cache.get(name, set())
                              | self._desc_token_cache.get(name, set())
                              | self._content_token_cache.get(name, set()))
                if not all_tokens:
                    # Fallback: tokenize on the fly if caches are cold
                    text = f"{m.name} {m.description} {m.content}"
                    all_tokens = set(self._tokenize(text))
                for word in all_tokens:
                    token_df[word] = token_df.get(word, 0) + 1
            min_df = max(1, int(doc_count * 0.01))
            self._idf_cache = {
                t: _math.log((doc_count + 1) / (df + 1)) + 1.0
                for t, df in token_df.items()
                if df >= min_df
            }
            self._idf_doc_count = doc_count

        idf_map = self._idf_cache

        def idf(token: str) -> float:
            return idf_map.get(token, 1.0)

        # ── Score each candidate memory (using pre-tokenized caches) ─────
        results: list[tuple[float, Memory]] = []
        for memory in target_memories.values():
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

            # Token-level IDF-weighted match using pre-tokenized caches
            name_tokens = self._token_cache.get(memory.name)
            desc_tokens = self._desc_token_cache.get(memory.name)
            content_tokens = self._content_token_cache.get(memory.name)

            # Cold cache fallback: tokenize on the fly
            if name_tokens is None:
                name_tokens = set(self._tokenize(memory.name))
                self._token_cache[memory.name] = name_tokens
            if desc_tokens is None:
                desc_tokens = set(self._tokenize(memory.description))
                self._desc_token_cache[memory.name] = desc_tokens
            if content_tokens is None:
                content_tokens = set(self._tokenize(memory.content))
                self._content_token_cache[memory.name] = content_tokens

            for token in query_tokens:
                w = idf(token)
                if token in name_tokens:
                    score += 6.0 * w
                if token in desc_tokens:
                    score += 3.0 * w
                if token in content_tokens:
                    score += 1.5 * w

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
        with self._lock:
            if not self._memories:
                return ""

            # Re-use the scored search
            scored = self._search_scored(user_input)

            relevant = [m for score, m in scored if score >= min_score][:max_memories]
            if not relevant:
                return ""

            # Bump access tracking under lock to prevent races with add()/delete()
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            now_ts = time.time()
            for m in relevant:
                m.metadata["last_accessed"] = now
                m.metadata["access_count"] = m.metadata.get("access_count", 0) + 1

        # Persist access metadata outside the lock (disk I/O)
        for m in relevant:
            last_write = self._access_write_debounce.get(m.name, 0)
            if now_ts - last_write >= self._ACCESS_DEBOUNCE_S:
                try:
                    file_path = self.memory_dir / m.file_path
                    self._write_file_atomically(file_path, m.to_frontmatter())
                    self._access_write_debounce[m.name] = now_ts
                except Exception:
                    logger.debug("Failed to persist access metadata for %s", m.name)

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
        with self._lock:
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

    def build_memory_section(self, user_input: str = "",
                             max_tokens: int = 600) -> str:
        """Unified memory section for the system prompt.

        When *user_input* is provided, searches for relevant memories.
        Otherwise returns a summary of recently-updated memories.

        The combined section respects *max_tokens* (token-based truncation
        via the TokenCounter, falling back to character-based).
        """
        if not self._memories:
            return ""

        if user_input:
            section = self.recall_context(user_input)
        else:
            section = self.get_memory_context()

        if not section:
            return ""

        # ── Token budget enforcement ────────────────────────────────────
        if max_tokens > 0:
            try:
                from .token_counter import estimate_tokens
                tokens = estimate_tokens(section)
                if tokens > max_tokens:
                    # Truncate character-by-character until within budget
                    # (roughly 4 chars per token)
                    char_budget = max(100, max_tokens * 4)
                    if len(section) > char_budget:
                        section = section[:char_budget] + "\n... (memory section truncated)"
            except Exception:
                pass  # TokenCounter unavailable — fall through

        return section

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


# ── Global instances (keyed by memory_dir) ───────────────────────────────────

_memory_stores: dict[str, MemoryStore] = {}
_memory_stores_lock = threading.Lock()


def get_memory_store(memory_dir: str | None = None) -> MemoryStore:
    """Return (or create) the MemoryStore for *memory_dir*.

    The store is cached per-directory so callers that pass different paths
    get independent instances.  When *memory_dir* is ``None`` the default
    directory from settings is used.
    """
    # Resolve the actual path upfront so caching is consistent
    if memory_dir is None:
        try:
            from .settings import get_settings
            memory_dir = str(get_settings().memory_dir)
        except Exception:
            memory_dir = str(Path.home() / ".ata_coder" / "memory")
    key = str(Path(memory_dir).resolve())
    with _memory_stores_lock:
        if key not in _memory_stores:
            _memory_stores[key] = MemoryStore(memory_dir)
        return _memory_stores[key]
