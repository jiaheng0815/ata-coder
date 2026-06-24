"""
Lightweight Python symbol index using ast — zero external dependencies.

Indexes functions, classes, methods, imports, and top-level assignments
from .py files.  Supports exact name lookup and prefix search.  Designed
to complement grep/glob with structure-aware code discovery.

Usage:
    idx = CodebaseIndex(Path("."))
    idx.build()                         # walk all .py files
    results = idx.search("handle_")     # prefix search
    results = idx.find_definition("CoderAgent")  # exact match
"""

import ast
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SKIP_DIRS = {"node_modules", "__pycache__", ".git", "venv", ".venv",
             "dist", "build", ".tox", ".eggs", ".mypy_cache", ".pytest_cache"}


@dataclass
class SymbolDef:
    """A symbol found in source code."""
    name: str
    kind: str          # function, method, class, import, variable
    file: str          # relative path
    line: int
    parent: str = ""   # class name for methods


@dataclass
class IndexResult:
    """Result of a search operation."""
    query: str
    matches: list[SymbolDef] = field(default_factory=list)
    total_files: int = 0
    total_symbols: int = 0


class CodebaseIndex:
    """AST-based symbol index for Python codebases.

    Walks .py files, parses each with ast, and extracts top-level
    and class-level definitions.  Results are cached in memory until
    rebuild() is called.
    """

    def __init__(self, root: Path | str = "."):
        self.root = Path(root).resolve()
        self._symbols: list[SymbolDef] = []
        self._by_name: dict[str, list[SymbolDef]] = {}
        self._file_count: int = 0

    # ── Build ─────────────────────────────────────────────────────────

    def build(self, max_files: int = 500) -> IndexResult:
        """Walk the project and index all .py files up to *max_files*."""
        self._symbols.clear()
        self._by_name.clear()
        self._file_count = 0

        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fname in filenames:
                if self._file_count >= max_files:
                    break
                if fname.endswith(".py"):
                    fpath = Path(dirpath) / fname
                    try:
                        self._index_file(fpath)
                        self._file_count += 1
                    except Exception:
                        logger.debug("Skipping %s (parse error)", fpath)

        logger.info("Indexed %d symbols across %d files",
                     len(self._symbols), self._file_count)
        return IndexResult(
            query="<build>",
            matches=self._symbols[:20],
            total_files=self._file_count,
            total_symbols=len(self._symbols),
        )

    def _index_file(self, filepath: Path) -> None:
        """Parse one .py file and extract definitions."""
        try:
            source = filepath.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return

        try:
            tree = ast.parse(source, filename=str(filepath))
        except SyntaxError:
            return

        rel = str(filepath.relative_to(self.root))
        for node in ast.iter_child_nodes(tree):
            self._extract_node(node, rel, "")

    def _extract_node(self, node: ast.AST, rel: str, parent: str) -> None:
        """Recursively extract symbol definitions from an AST node."""
        if isinstance(node, ast.FunctionDef):
            kind = "method" if parent else "function"
            sd = SymbolDef(node.name, kind, rel, node.lineno, parent)
            self._add(sd)
            for child in ast.iter_child_nodes(node):
                self._extract_node(child, rel, node.name)

        elif isinstance(node, ast.ClassDef):
            sd = SymbolDef(node.name, "class", rel, node.lineno, parent)
            self._add(sd)
            for child in ast.iter_child_nodes(node):
                self._extract_node(child, rel, node.name)

        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name
                self._add(SymbolDef(name, "import", rel, node.lineno, parent))

        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                name = alias.asname or alias.name
                self._add(SymbolDef(name, "import", rel, node.lineno, parent))

        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self._add(SymbolDef(target.id, "variable", rel, node.lineno, parent))

    def _add(self, sd: SymbolDef) -> None:
        self._symbols.append(sd)
        key = sd.name.lower()
        if key not in self._by_name:
            self._by_name[key] = []
        self._by_name[key].append(sd)

    # ── Search ────────────────────────────────────────────────────────

    def search(self, query: str, kind: str = "",
               max_results: int = 50) -> IndexResult:
        """Search symbols by name (case-insensitive prefix or exact match).

        Args:
            query: symbol name or prefix
            kind: filter by kind (function, class, method, import, variable)
            max_results: cap on returned matches
        """
        q = query.lower().strip()
        if not q:
            return IndexResult(query=query)

        matches: list[SymbolDef] = []
        # Exact match first
        if q in self._by_name:
            matches.extend(self._by_name[q])
        # Then prefix matches
        for key, syms in self._by_name.items():
            if key.startswith(q) and key != q:
                matches.extend(syms)

        if kind:
            matches = [m for m in matches if m.kind == kind]

        return IndexResult(
            query=query,
            matches=matches[:max_results],
            total_files=self._file_count,
            total_symbols=len(self._symbols),
        )

    def find_definition(self, name: str) -> list[SymbolDef]:
        """Exact case-insensitive definition lookup."""
        return self._by_name.get(name.lower().strip(), [])

    # ── Info ──────────────────────────────────────────────────────────

    @property
    def stats(self) -> dict:
        kinds: dict[str, int] = {}
        for s in self._symbols:
            kinds[s.kind] = kinds.get(s.kind, 0) + 1
        return {
            "files": self._file_count,
            "symbols": len(self._symbols),
            "by_kind": kinds,
        }
