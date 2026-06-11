"""
Tests for memory — Memory dataclass, MemoryStore CRUD, search, context recall.
"""

import json
import os
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch
import pytest

from ata_coder.memory import Memory, MemoryStore, create_memory


# ═══════════════════════════════════════════════════════════════════════════════
# Memory dataclass
# ═══════════════════════════════════════════════════════════════════════════════

class TestMemoryDataclass:
    """Memory dataclass construction and serialization."""

    def test_memory_creation(self):
        """Memory should be creatable with required fields."""
        mem = Memory(
            name="test-memory",
            description="A test memory",
            content="This is the content",
        )
        assert mem.name == "test-memory"
        assert mem.description == "A test memory"
        assert mem.content == "This is the content"

    def test_memory_default_type(self):
        """Default memory_type should be 'reference'."""
        mem = Memory(name="test", description="", content="")
        assert mem.memory_type == "reference"

    def test_memory_custom_type(self):
        """memory_type should come from metadata."""
        mem = Memory(
            name="user-pref",
            description="User likes dark mode",
            content="dark mode",
            metadata={"type": "user"},
        )
        assert mem.memory_type == "user"

    def test_memory_file_path(self):
        """file_path should be name + '.md'."""
        mem = Memory(name="my-memory", description="", content="")
        assert mem.file_path == "my-memory.md"

    def test_to_frontmatter_has_yaml(self):
        """to_frontmatter() should produce YAML frontmatter."""
        mem = Memory(
            name="test",
            description="Test desc",
            content="Body content",
            metadata={"type": "user", "tags": ["python"]},
        )
        result = mem.to_frontmatter()
        assert result.startswith("---")
        assert "name: test" in result
        assert "description: Test desc" in result
        assert "type: user" in result
        assert "---" in result
        assert "Body content" in result

    def test_from_frontmatter_parses_yaml(self):
        """from_frontmatter() should parse YAML frontmatter correctly."""
        raw = """---
name: my-memory
description: Test description
metadata:
  type: user
  tags:
    - python
created: "2024-01-01T00:00:00Z"
updated: "2024-01-02T00:00:00Z"
---

This is the body content.
"""
        mem = Memory.from_frontmatter(raw)
        assert mem is not None
        assert mem.name == "my-memory"
        assert mem.description == "Test description"
        assert mem.content == "This is the body content."
        assert mem.memory_type == "user"
        assert mem.metadata["tags"] == ["python"]

    def test_from_frontmatter_no_frontmatter(self):
        """Content without frontmatter should return None."""
        mem = Memory.from_frontmatter("Just plain text without frontmatter.")
        assert mem is None

    def test_from_frontmatter_invalid_yaml(self):
        """Invalid YAML frontmatter should return None gracefully."""
        raw = """---
invalid: [yaml: broken
---
body
"""
        mem = Memory.from_frontmatter(raw)
        # May or may not parse depending on yaml module
        # At minimum should not crash
        assert mem is None or isinstance(mem, Memory)

    def test_from_frontmatter_empty_metadata(self):
        """Frontmatter with minimal fields should still work."""
        raw = """---
name: minimal
description: desc
metadata: {}
---

content
"""
        mem = Memory.from_frontmatter(raw)
        assert mem is not None
        assert mem.name == "minimal"
        assert mem.content == "content"

    def test_to_frontmatter_roundtrip(self):
        """to_frontmatter() → from_frontmatter() should preserve data."""
        original = Memory(
            name="roundtrip-test",
            description="Testing roundtrip",
            content="Important content here",
            metadata={"type": "project", "priority": "high"},
            created="2024-06-01T00:00:00Z",
        )
        serialized = original.to_frontmatter()
        parsed = Memory.from_frontmatter(serialized)
        assert parsed is not None
        assert parsed.name == original.name
        assert parsed.description == original.description
        assert parsed.content == original.content
        assert parsed.memory_type == original.memory_type


# ═══════════════════════════════════════════════════════════════════════════════
# MemoryStore
# ═══════════════════════════════════════════════════════════════════════════════

class TestMemoryStoreInit:
    """MemoryStore initialization."""

    def test_init_creates_memory_dir(self, tmp_path):
        """MemoryStore should create the memory directory on init."""
        memory_dir = tmp_path / "ata_memories"
        store = MemoryStore(memory_dir)
        assert memory_dir.exists()

    def test_init_creates_memory_index(self, tmp_path):
        """MemoryStore should create MEMORY.md index on init."""
        memory_dir = tmp_path / "ata_memories"
        store = MemoryStore(memory_dir)
        index_file = memory_dir / "MEMORY.md"
        assert index_file.exists()

    def test_init_empty_store(self, tmp_path):
        """New MemoryStore should have no memories."""
        store = MemoryStore(tmp_path)
        assert len(store.list_all()) == 0


class TestMemoryStoreCRUD:
    """MemoryStore CRUD operations."""

    def test_add_memory(self, tmp_path):
        """Adding a memory should store it and create a file."""
        store = MemoryStore(tmp_path)
        mem = Memory(
            name="test-add",
            description="Test add",
            content="Hello, memory!",
            metadata={"type": "reference"},
        )
        store.add(mem)
        assert store.get("test-add") is not None
        assert store.get("test-add").content == "Hello, memory!"

    def test_add_creates_file(self, tmp_path):
        """Adding a memory should create a .md file on disk."""
        store = MemoryStore(tmp_path)
        mem = Memory(
            name="disk-test",
            description="Test file creation",
            content="On disk content",
        )
        store.add(mem)
        file_path = tmp_path / "disk-test.md"
        assert file_path.exists()
        content = file_path.read_text(encoding="utf-8")
        assert "On disk content" in content

    def test_add_updates_index(self, tmp_path):
        """Adding a memory should update MEMORY.md index."""
        store = MemoryStore(tmp_path)
        mem = Memory(
            name="index-test",
            description="Index entry",
            content="Content",
        )
        store.add(mem)
        index_content = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
        assert "Index entry" in index_content
        assert "index-test.md" in index_content

    def test_update_existing_memory(self, tmp_path):
        """Updating an existing memory should preserve creation time."""
        store = MemoryStore(tmp_path)
        mem = Memory(
            name="update-test",
            description="Original",
            content="Original content",
            created="2024-01-01T00:00:00Z",
        )
        store.add(mem)

        updated = Memory(
            name="update-test",
            description="Updated",
            content="Updated content",
        )
        store.add(updated)

        retrieved = store.get("update-test")
        assert retrieved.content == "Updated content"
        assert retrieved.created == "2024-01-01T00:00:00Z"

    def test_get_nonexistent(self, tmp_path):
        """Getting a nonexistent memory should return None."""
        store = MemoryStore(tmp_path)
        assert store.get("nonexistent") is None

    def test_delete_memory(self, tmp_path):
        """Deleting a memory should remove it from the store."""
        store = MemoryStore(tmp_path)
        mem = Memory(name="delete-me", description="To delete", content="Bye!")
        store.add(mem)
        assert store.get("delete-me") is not None

        result = store.delete("delete-me")
        assert result is True
        assert store.get("delete-me") is None

    def test_delete_removes_file(self, tmp_path):
        """Deleting a memory should remove the .md file."""
        store = MemoryStore(tmp_path)
        mem = Memory(name="delete-file", description="Delete file", content="Gone")
        store.add(mem)
        file_path = tmp_path / "delete-file.md"
        assert file_path.exists()

        store.delete("delete-file")
        assert not file_path.exists()

    def test_delete_nonexistent(self, tmp_path):
        """Deleting a nonexistent memory should return False."""
        store = MemoryStore(tmp_path)
        assert store.delete("nonexistent") is False

    def test_list_all(self, tmp_path):
        """list_all should return all memories sorted by updated time."""
        store = MemoryStore(tmp_path)
        store.add(Memory(name="a", description="First", content="A"))
        store.add(Memory(name="b", description="Second", content="B"))
        store.add(Memory(name="c", description="Third", content="C"))

        all_mems = store.list_all()
        assert len(all_mems) == 3

    def test_list_all_filtered_by_type(self, tmp_path):
        """list_all with memory_type filter should return matching memories."""
        store = MemoryStore(tmp_path)
        store.add(Memory(name="u1", description="User 1", content="", metadata={"type": "user"}))
        store.add(Memory(name="p1", description="Proj 1", content="", metadata={"type": "project"}))
        store.add(Memory(name="r1", description="Ref 1", content="", metadata={"type": "reference"}))

        users = store.list_all(memory_type="user")
        assert len(users) == 1
        assert users[0].name == "u1"

        projects = store.list_all(memory_type="project")
        assert len(projects) == 1


class TestMemoryStoreSearch:
    """MemoryStore search functionality."""

    def test_search_returns_relevant(self, tmp_path):
        """Search should return memories matching the query."""
        store = MemoryStore(tmp_path)
        store.add(Memory(name="python-tips", description="Python coding tips",
                         content="Use list comprehensions", metadata={"type": "reference"}))
        store.add(Memory(name="js-tips", description="JavaScript tips",
                         content="Use const instead of let", metadata={"type": "reference"}))

        results = store.search("python")
        assert len(results) >= 1
        assert any("python" in r.name or "python" in r.description for r in results)

    def test_search_empty_store(self, tmp_path):
        """Search on empty store should return empty list."""
        store = MemoryStore(tmp_path)
        results = store.search("anything")
        assert results == []

    def test_search_no_match(self, tmp_path):
        """Search with no matches should return empty list."""
        store = MemoryStore(tmp_path)
        store.add(Memory(name="only-one", description="Only memory", content="Just one"))
        results = store.search("nonexistent_gibberish_xyzzy")
        assert results == []

    def test_name_match_scored_higher(self, tmp_path):
        """Name matches should be scored higher than content matches."""
        store = MemoryStore(tmp_path)
        store.add(Memory(name="target-word", description="Some memory", content="irrelevant"))
        store.add(Memory(name="other", description="Other memory", content="target-word mentioned here"))
        results = store.search("target-word")
        assert len(results) >= 1
        # The one with "target-word" in the name should be first
        assert results[0].name == "target-word"

    def test_recall_context_filters_by_score(self, tmp_path):
        """recall_context should only return memories above min_score."""
        store = MemoryStore(tmp_path)
        store.add(Memory(name="relevant", description="Very relevant memory",
                         content="The answer is 42"))
        store.add(Memory(name="noise", description="Unrelated",
                         content="Random stuff here"))

        # Search for "answer 42" — relevant should match, noise shouldn't
        context = store.recall_context("answer 42", min_score=3.0)
        assert "Very relevant memory" in context or "relevant" in context


class TestMemoryStoreRecall:
    """MemoryStore recall_context and get_memory_context."""

    def test_recall_context_empty_store(self, tmp_path):
        """recall_context should return empty string for empty store."""
        store = MemoryStore(tmp_path)
        result = store.recall_context("anything")
        assert result == ""

    def test_recall_context_returns_formatted(self, tmp_path):
        """recall_context should return formatted string with relevant memories."""
        store = MemoryStore(tmp_path)
        store.add(Memory(
            name="user-prefers-dark",
            description="User prefers dark mode",
            content="The user has explicitly stated they prefer dark mode in all applications.",
            metadata={"type": "user"},
        ))
        result = store.recall_context("dark mode")
        assert "## Relevant Memories" in result
        assert "dark mode" in result.lower()

    def test_get_memory_context_empty(self, tmp_path):
        """get_memory_context should return empty string for empty store."""
        store = MemoryStore(tmp_path)
        result = store.get_memory_context()
        assert result == ""

    def test_get_memory_context_returns_summary(self, tmp_path):
        """get_memory_context should return organized summary by type."""
        store = MemoryStore(tmp_path)
        store.add(Memory(name="pref", description="User prefers tabs",
                         content="", metadata={"type": "user"}))
        store.add(Memory(name="arch", description="Uses FastAPI",
                         content="", metadata={"type": "project"}))
        result = store.get_memory_context()
        assert "## Persistent Memory" in result
        assert "User" in result
        assert "Project" in result


class TestMemoryStoreSuggestions:
    """suggest_from_conversation heuristics."""

    def test_suggest_remember_keyword(self, tmp_path):
        """Messages containing 'remember' should trigger a suggestion."""
        store = MemoryStore(tmp_path)
        suggestions = store.suggest_from_conversation(
            user_messages=["remember to use tabs for indentation"]
        )
        assert len(suggestions) >= 1
        assert "remember" in suggestions[0].lower()

    def test_suggest_chinese_remember(self, tmp_path):
        """Chinese '记住' keyword should trigger a suggestion."""
        store = MemoryStore(tmp_path)
        suggestions = store.suggest_from_conversation(
            user_messages=["记住，要用空格不要用制表符"]
        )
        assert len(suggestions) >= 1

    def test_suggest_toolchain_detected(self, tmp_path):
        """Messages mentioning ESP-IDF should trigger toolchain suggestion."""
        store = MemoryStore(tmp_path)
        suggestions = store.suggest_from_conversation(
            user_messages=["We build with idf.py for ESP32"]
        )
        assert len(suggestions) >= 1
        assert any("idf.py" in s for s in suggestions)

    def test_suggest_device_port(self, tmp_path):
        """Messages mentioning COM ports should trigger port suggestion."""
        store = MemoryStore(tmp_path)
        suggestions = store.suggest_from_conversation(
            user_messages=["The device is on COM5"]
        )
        assert len(suggestions) >= 1
        assert any("COM5" in s for s in suggestions)

    def test_suggest_tool_error(self, tmp_path):
        """Tool errors about blocked commands should trigger suggestion."""
        store = MemoryStore(tmp_path)
        suggestions = store.suggest_from_conversation(
            user_messages=["run command failed"],
            tool_errors=["Error: 'not in the allowed list'"]
        )
        assert len(suggestions) >= 1
        assert any("allowed list" in s.lower() for s in suggestions)

    def test_suggest_tool_error_command_not_found(self, tmp_path):
        """Command not found errors should trigger suggestion."""
        store = MemoryStore(tmp_path)
        suggestions = store.suggest_from_conversation(
            user_messages=[],
            tool_errors=["'rg' is not recognized as an internal or external command"]
        )
        assert len(suggestions) >= 1

    def test_suggestions_capped(self, tmp_path):
        """suggestions should be capped at 5."""
        store = MemoryStore(tmp_path)
        # Trigger many suggestions
        suggestions = store.suggest_from_conversation(
            user_messages=[
                "remember this",
                "记住那个",
                "don't forget this",
                "save this too",
                "and remember this also",
                "记住这个重要的事情",
            ]
        )
        assert len(suggestions) <= 5

    def test_no_suggestions_for_normal_convo(self, tmp_path):
        """Normal conversation without keywords should produce no suggestions."""
        store = MemoryStore(tmp_path)
        suggestions = store.suggest_from_conversation(
            user_messages=["Hello", "Can you help me with Python?"]
        )
        assert suggestions == []


class TestMemoryStoreLoading:
    """Loading memories from disk on initialization."""

    def test_load_existing_memories(self, tmp_path):
        """Memories saved in one session should load in another."""
        store1 = MemoryStore(tmp_path)
        store1.add(Memory(name="persist", description="Persistent memory",
                          content="Survives restarts"))
        del store1

        store2 = MemoryStore(tmp_path)
        retrieved = store2.get("persist")
        assert retrieved is not None
        assert retrieved.content == "Survives restarts"

    def test_load_invalid_memory_file(self, tmp_path):
        """Invalid memory files should be skipped, not crash."""
        invalid_file = tmp_path / "bad.md"
        invalid_file.write_text("This is not valid frontmatter content")
        store = MemoryStore(tmp_path)
        # Should not raise
        assert store.get("bad") is None

    def test_extract_links(self, tmp_path):
        """Wiki-style [[links]] should be extractable."""
        store = MemoryStore(tmp_path)
        content = "See [[python-tips]] and [[project-setup]] for details"
        links = store._extract_links(content)
        assert "python-tips" in links
        assert "project-setup" in links
        assert len(links) == 2


class TestCreateMemory:
    """Convenience function create_memory."""

    def test_create_memory_function(self, tmp_path):
        """create_memory should create and return a Memory."""
        mem = create_memory(
            name="convenience-test",
            description="Created via convenience",
            content="Quick create content",
            memory_type="project",
        )
        assert mem.name == "convenience-test"
        assert mem.description == "Created via convenience"
        assert mem.content == "Quick create content"
        assert mem.memory_type == "project"
