"""Tests for the RAG-based long context memory."""

import tempfile
from pathlib import Path

import pytest

from ata_coder.rag_memory import RAGIndex, Chunk, SearchResult, _tokenize, get_rag_index


class TestTokenize:
    """Tokenization for TF-IDF."""

    def test_basic_tokens(self):
        tokens = _tokenize("hello world test")
        assert "hello" in tokens
        assert "world" in tokens
        assert "test" in tokens

    def test_stopwords_removed(self):
        tokens = _tokenize("the and or but is are was")
        assert not any(t in _STOPWORDS_CORE for t in tokens)

    def test_short_tokens_removed(self):
        tokens = _tokenize("a x b c d")
        assert len(tokens) == 0  # All are stopwords or < 2 chars

    def test_code_tokens(self):
        tokens = _tokenize("def authenticate_user(token: str) -> bool")
        # Underscore-connected identifiers stay as one token
        assert "authenticate_user" in tokens
        assert "token" in tokens
        assert "str" in tokens
        assert "bool" in tokens


_STOPWORDS_CORE = {"the", "and", "or", "but", "is", "are", "was", "were", "be", "been",
                   "a", "an", "in", "on", "at", "to", "for", "of", "with", "by", "from"}


class TestRAGIndex:
    """RAGIndex tests with a small temp project."""

    @pytest.fixture
    def temp_project(self):
        """Create a temp workspace with some Python files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            # Create a simple Python file
            src = base / "src"
            src.mkdir()
            (src / "auth.py").write_text("""\"\"\"Authentication module.\"\"\"

def login(username: str, password: str) -> bool:
    \"\"\"Authenticate a user.\"\"\"
    if not username or not password:
        raise ValueError("Credentials required")
    return username == "admin" and password == "secret"


def logout(session_id: str) -> None:
    \"\"\"End a user session.\"\"\"
    sessions.pop(session_id, None)


class UserSession:
    \"\"\"Manages an authenticated session.\"\"\"

    def __init__(self, user: str):
        self.user = user
        self.created_at = None

    def is_expired(self) -> bool:
        return False
""")
            (src / "utils.py").write_text("""\"\"\"Utility functions.\"\"\"

def format_date(ts: float) -> str:
    \"\"\"Format a timestamp.\"\"\"
    from datetime import datetime
    return datetime.fromtimestamp(ts).isoformat()


def chunk_list(items: list, size: int = 10) -> list:
    \"\"\"Split a list into fixed-size chunks.\"\"\"
    return [items[i:i+size] for i in range(0, len(items), size)]
""")
            yield base

    def test_index_project(self, temp_project):
        """Index a small project and verify chunk count."""
        rag = RAGIndex(workspace_dir=temp_project)
        count = rag.index_project()
        assert count > 0
        assert rag._indexed

    def test_chunk_python_functions(self, temp_project):
        """Python files are chunked by function/class."""
        rag = RAGIndex(workspace_dir=temp_project)
        rag.index_project()
        # Should have chunks for login, logout, UserSession, __init__, is_expired,
        # format_date, chunk_list, plus module-level chunks
        functions = [c for c in rag._chunks.values() if c.kind == "function"]
        assert len(functions) >= 5  # login, logout, __init__, is_expired, format_date, chunk_list
        names = {c.name for c in functions}
        assert "login" in names
        assert "logout" in names
        assert "format_date" in names
        assert "chunk_list" in names

    def test_chunk_python_classes(self, temp_project):
        """Classes are detected as a separate kind."""
        rag = RAGIndex(workspace_dir=temp_project)
        rag.index_project()
        classes = [c for c in rag._chunks.values() if c.kind == "class"]
        assert len(classes) >= 1
        assert any(c.name == "UserSession" for c in classes)

    def test_search_finds_relevant_code(self, temp_project):
        """TF-IDF search returns relevant chunks."""
        rag = RAGIndex(workspace_dir=temp_project)
        rag.index_project()
        results = rag.search("authentication login", top_k=5)
        assert len(results) > 0
        # Auth-related results should come first
        top_file = results[0].chunk.file_path
        assert "auth" in top_file.lower()

    def test_search_no_results(self, temp_project):
        """Search for nonsense returns empty."""
        rag = RAGIndex(workspace_dir=temp_project)
        rag.index_project()
        results = rag.search("xyznonexistent12345")
        assert len(results) == 0

    def test_get_context_formatted(self, temp_project):
        """get_context returns formatted markdown."""
        rag = RAGIndex(workspace_dir=temp_project)
        rag.index_project()
        ctx = rag.get_context("login authentication", top_k=3)
        assert "Codebase Context" in ctx
        assert "auth.py" in ctx
        assert "```" in ctx

    def test_get_relevant_files(self, temp_project):
        """get_relevant_files returns just paths."""
        rag = RAGIndex(workspace_dir=temp_project)
        rag.index_project()
        files = rag.get_relevant_files("date format", top_k=2)
        assert len(files) > 0
        assert any("utils" in f for f in files)

    def test_reindex_force(self, temp_project):
        """Force reindex clears and rebuilds."""
        rag = RAGIndex(workspace_dir=temp_project)
        first = rag.index_project()
        second = rag.index_project(force=True)
        assert first == second

    def test_skip_dirs_ignored(self, temp_project):
        """.git, __pycache__, etc. are skipped."""
        rag = RAGIndex(workspace_dir=temp_project)
        rag.index_project()
        for chunk in rag._chunks.values():
            assert ".git" not in chunk.file_path
            assert "__pycache__" not in chunk.file_path

    def test_chunk_hash_id_stable(self, temp_project):
        """Chunk IDs are deterministic."""
        rag = RAGIndex(workspace_dir=temp_project)
        id1 = rag._hash_id("src/auth.py", 5)
        id2 = rag._hash_id("src/auth.py", 5)
        assert id1 == id2
        assert len(id1) == 16

    def test_empty_workspace(self):
        """Indexing an empty dir should not crash."""
        with tempfile.TemporaryDirectory() as empty:
            rag = RAGIndex(workspace_dir=empty)
            count = rag.index_project()
            assert count == 0
            assert rag.search("anything") == []


class TestSearchResult:
    """SearchResult dataclass."""

    def test_basic(self):
        chunk = Chunk(id="abc", file_path="x.py", start_line=1, end_line=10,
                       kind="function", name="foo", content="bar")
        sr = SearchResult(chunk=chunk, score=0.85, match_type="tfidf")
        assert sr.score == 0.85
        assert sr.match_type == "tfidf"
        assert sr.chunk.name == "foo"
