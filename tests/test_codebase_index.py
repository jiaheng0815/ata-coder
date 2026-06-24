"""Tests for codebase_index — AST-based Python symbol indexing."""

from pathlib import Path
from ata_coder.codebase_index import CodebaseIndex, SymbolDef, IndexResult


class TestIndexOnSelf:
    """Index the ata-coder project itself and verify results."""

    def test_build_discovers_agent(self):
        idx = CodebaseIndex(Path("."))
        result = idx.build(max_files=100)
        assert result.total_files > 0
        assert result.total_symbols > 0

    def test_find_coder_agent_class(self):
        idx = CodebaseIndex(Path("."))
        idx.build(max_files=100)
        defs = idx.find_definition("CoderAgent")
        assert len(defs) >= 1
        assert any(d.kind == "class" for d in defs)

    def test_search_prefix(self):
        idx = CodebaseIndex(Path("."))
        idx.build(max_files=100)
        result = idx.search("tool_")
        assert len(result.matches) > 0
        assert all("tool_" in m.name.lower() for m in result.matches)

    def test_search_by_kind(self):
        idx = CodebaseIndex(Path("."))
        idx.build(max_files=100)
        result = idx.search("test_", kind="function")
        for m in result.matches:
            assert m.kind == "function"

    def test_stats(self):
        idx = CodebaseIndex(Path("."))
        idx.build(max_files=50)
        stats = idx.stats
        assert "files" in stats
        assert "symbols" in stats
        assert "by_kind" in stats
        assert isinstance(stats["by_kind"], dict)


class TestIndexOnScratch:
    """Create a small Python file and index it."""

    def test_index_scratch_file(self, tmp_path):
        code = """\"\"\"Test module.\"\"\"
import os
from pathlib import Path

CONSTANT = 42

class MyClass:
    def method_one(self):
        pass

    def method_two(self, x):
        return x

def top_level_func():
    return MyClass()
"""
        f = tmp_path / "test_mod.py"
        f.write_text(code)
        idx = CodebaseIndex(tmp_path)
        idx.build()

        # Find the class
        defs = idx.find_definition("MyClass")
        assert len(defs) == 1
        assert defs[0].kind == "class"
        assert "test_mod.py" in defs[0].file

        # Find a method
        result = idx.search("method_one")
        methods = [m for m in result.matches if m.name == "method_one"]
        assert len(methods) == 1
        assert methods[0].kind == "method"
        assert methods[0].parent == "MyClass"

        # Imports
        imports = idx.search("os", kind="import")
        assert any(m.name == "os" for m in imports.matches)

        # Variables
        result = idx.search("CONSTANT")
        assert any(m.name == "CONSTANT" and m.kind == "variable"
                   for m in result.matches)

    def test_empty_search(self):
        idx = CodebaseIndex(Path("."))
        idx.build(max_files=10)
        result = idx.search("")
        assert result.query == ""
        assert result.matches == []

    def test_nonexistent_symbol(self):
        idx = CodebaseIndex(Path("."))
        idx.build(max_files=10)
        result = idx.search("this_symbol_does_not_exist_xyz")
        assert result.matches == []
