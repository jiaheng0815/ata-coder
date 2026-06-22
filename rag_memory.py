"""
RAG-based long context memory — semantic codebase search.

Enables the agent to "remember" the entire codebase beyond the current
conversation window by indexing project files into searchable chunks.

Features:
- Project file chunking (functions, classes, sections)
- TF-IDF semantic search (zero extra dependencies)
- Optional sentence-transformers for better embeddings
- Integration with existing MemoryStore for persistence
- Automatic context injection into system prompt

Usage:
    from .rag_memory import RAGIndex

    rag = RAGIndex(workspace_dir=".")
    rag.index_project()                    # chunk + embed all project files
    results = rag.search("auth login")     # semantic search
    context = rag.get_context("auth")      # formatted context for prompt
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Optional sentence-transformers import
_HAS_SENTENCE_TRANSFORMERS = False
_SentenceTransformer = None


def _check_st() -> bool:
    global _HAS_SENTENCE_TRANSFORMERS, _SentenceTransformer
    if _HAS_SENTENCE_TRANSFORMERS:
        return True
    try:
        from sentence_transformers import SentenceTransformer as ST
        _SentenceTransformer = ST
        _HAS_SENTENCE_TRANSFORMERS = True
        return True
    except ImportError:
        return False


# ── Data types ───────────────────────────────────────────────────────────────

@dataclass
class Chunk:
    """A searchable chunk of code or documentation."""
    id: str                          # hash-based unique ID
    file_path: str                   # relative path in the project
    start_line: int                  # 1-based
    end_line: int                    # 1-based
    kind: str                        # function | class | module | section
    name: str                        # function/class name or heading
    content: str                     # the actual text
    summary: str = ""                # first line / docstring


@dataclass
class SearchResult:
    """A ranked search result."""
    chunk: Chunk
    score: float
    match_type: str = "tfidf"       # tfidf | embedding | hybrid


# ── English stopwords ────────────────────────────────────────────────────────

_STOPWORDS: set[str] = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "can", "shall", "this", "that",
    "these", "those", "it", "its", "i", "me", "my", "we", "our", "you",
    "your", "he", "she", "they", "them", "not", "no", "as", "if", "so",
    "than", "also", "very", "just", "about", "into", "over", "after",
    "self", "def", "class", "import", "return", "pass", "none", "true",
    "false", "elif", "else", "try", "except", "finally", "raise", "yield",
}


def _tokenize(text: str) -> list[str]:
    """Tokenize: lowercase, strip punctuation, filter stopwords."""
    clean = re.sub(r'[^\w\s-]', ' ', text.lower())
    tokens = []
    for token in clean.split():
        token = token.strip('-_')
        if len(token) < 2:
            continue
        if token in _STOPWORDS:
            continue
        tokens.append(token)
    return tokens


# ── RAG Index ────────────────────────────────────────────────────────────────

class RAGIndex:
    """Semantic codebase index with TF-IDF + optional embedding search.

    Indexes a workspace directory into code chunks (functions, classes,
    sections) and provides fast semantic search to find relevant context
    for any query.
    """

    # File extensions to index
    CODE_EXTS = {".py", ".js", ".ts", ".tsx", ".jsx", ".rs", ".go", ".java",
                 ".c", ".cpp", ".h", ".hpp", ".rb", ".php", ".swift", ".kt",
                 ".yaml", ".yml", ".toml", ".json", ".md", ".rst", ".txt",
                 ".sh", ".bash", ".ps1", ".sql", ".graphql"}

    # Skip these directories
    SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv",
                 ".tox", ".eggs", "dist", "build", ".pytest_cache",
                 ".mypy_cache", ".ruff_cache", "egg-info", ".claude",
                 ".ata_coder"}

    # Max file size to index (bytes)
    MAX_FILE_SIZE = 500_000

    def __init__(self, workspace_dir: str | Path = "."):
        self._workspace = Path(workspace_dir).resolve()
        self._chunks: dict[str, Chunk] = {}
        self._file_hashes: dict[str, str] = {}  # path → content hash
        self._lock = threading.Lock()
        self._indexed = False

        # Embedding model (lazy)
        self._embedder = None
        self._embeddings: dict[str, list[float]] = {}  # chunk_id → vector

        # TF-IDF cache
        self._idf: dict[str, float] = {}
        self._chunk_tokens: dict[str, set[str]] = {}

    # ── Indexing ─────────────────────────────────────────────────────────

    @property
    def chunk_count(self) -> int:
        return len(self._chunks)

    def index_project(self, force: bool = False) -> int:
        """Index all code files in the workspace. Returns chunk count."""
        if self._indexed and not force:
            return self.chunk_count

        logger.info("Indexing project: %s", self._workspace)
        new_chunks: dict[str, Chunk] = {}
        files_scanned = 0

        for file_path in self._walk_files():
            files_scanned += 1
            try:
                chunks = self._chunk_file(file_path)
                for c in chunks:
                    new_chunks[c.id] = c
            except Exception:
                logger.debug("Failed to chunk: %s", file_path)

        with self._lock:
            self._chunks = new_chunks
            self._indexed = True
            # Rebuild TF-IDF
            self._build_idf()

        logger.info("Indexed %d files → %d chunks", files_scanned, len(new_chunks))
        return len(new_chunks)

    def _walk_files(self) -> list[Path]:
        """Walk workspace, yield file paths to index."""
        result: list[Path] = []
        for root, dirs, files in os.walk(self._workspace):
            # Prune skip dirs
            dirs[:] = [d for d in dirs if d not in self.SKIP_DIRS]
            for fname in files:
                fpath = Path(root) / fname
                if fpath.suffix in self.CODE_EXTS:
                    if fpath.stat().st_size <= self.MAX_FILE_SIZE:
                        result.append(fpath)
        return result

    def _chunk_file(self, file_path: Path) -> list[Chunk]:
        """Split a source file into semantic chunks."""
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return []

        rel_path = str(file_path.relative_to(self._workspace)).replace("\\", "/")
        lines = text.split("\n")
        ext = file_path.suffix

        if ext == ".py":
            return self._chunk_python(rel_path, lines)
        if ext in (".md", ".rst", ".txt"):
            return self._chunk_markdown(rel_path, lines)
        return self._chunk_generic(rel_path, lines)

    def _chunk_python(self, rel_path: str, lines: list[str]) -> list[Chunk]:
        """Chunk a Python file by functions and classes."""
        chunks: list[Chunk] = []
        current_start = 0
        current_kind = "module"
        current_name = rel_path.split("/")[-1]

        for i, line in enumerate(lines):
            stripped = line.strip()
            # Detect function/class definitions
            func_match = re.match(r'^\s*def\s+(\w+)', stripped)
            class_match = re.match(r'^\s*class\s+(\w+)', stripped)

            if func_match or class_match:
                # Save previous chunk
                if i > current_start:
                    chunk_lines = lines[current_start:i]
                    content = "\n".join(chunk_lines).strip()
                    if content and not all(l.strip().startswith(("#", "from ", "import ")) for l in chunk_lines if l.strip()):
                        cid = self._hash_id(rel_path, current_start)
                        chunks.append(Chunk(
                            id=cid, file_path=rel_path,
                            start_line=current_start + 1, end_line=i,
                            kind=current_kind, name=current_name,
                            content=content,
                            summary=self._extract_summary(content),
                        ))
                current_start = i
                if func_match:
                    current_kind = "function"
                    current_name = func_match.group(1)
                else:
                    current_kind = "class"
                    current_name = class_match.group(1)

        # Final chunk
        if current_start < len(lines):
            content = "\n".join(lines[current_start:]).strip()
            if content:
                cid = self._hash_id(rel_path, current_start)
                chunks.append(Chunk(
                    id=cid, file_path=rel_path,
                    start_line=current_start + 1, end_line=len(lines),
                    kind=current_kind, name=current_name,
                    content=content,
                    summary=self._extract_summary(content),
                ))

        return chunks

    def _chunk_markdown(self, rel_path: str, lines: list[str]) -> list[Chunk]:
        """Chunk markdown by headings."""
        chunks: list[Chunk] = []
        current_start = 0
        current_name = rel_path.split("/")[-1]

        for i, line in enumerate(lines):
            heading = re.match(r'^#{1,4}\s+(.+)', line.strip())
            if heading and i > 0:
                content = "\n".join(lines[current_start:i]).strip()
                if content:
                    cid = self._hash_id(rel_path, current_start)
                    chunks.append(Chunk(
                        id=cid, file_path=rel_path,
                        start_line=current_start + 1, end_line=i,
                        kind="section", name=current_name,
                        content=content,
                        summary=heading.group(1),
                    ))
                current_start = i
                current_name = heading.group(1)

        # Final chunk
        if current_start < len(lines):
            content = "\n".join(lines[current_start:]).strip()
            if content:
                cid = self._hash_id(rel_path, current_start)
                chunks.append(Chunk(
                    id=cid, file_path=rel_path,
                    start_line=current_start + 1, end_line=len(lines),
                    kind="section", name=current_name,
                    content=content,
                    summary=self._extract_summary(content),
                ))
        return chunks

    def _chunk_generic(self, rel_path: str, lines: list[str]) -> list[Chunk]:
        """Chunk a generic file into ~100-line segments."""
        chunks: list[Chunk] = []
        chunk_size = 100
        for start in range(0, len(lines), chunk_size):
            end = min(start + chunk_size, len(lines))
            content = "\n".join(lines[start:end]).strip()
            if not content:
                continue
            cid = self._hash_id(rel_path, start)
            chunks.append(Chunk(
                id=cid, file_path=rel_path,
                start_line=start + 1, end_line=end,
                kind="file", name=rel_path.split("/")[-1],
                content=content,
                summary=self._extract_summary(content),
            ))
        return chunks

    @staticmethod
    def _extract_summary(content: str) -> str:
        """Extract a one-line summary (first non-empty, non-comment line)."""
        for line in content.split("\n"):
            stripped = line.strip()
            if stripped and not stripped.startswith(("#", "//", "/*", "*", "<!--")):
                return stripped[:120]
        return content[:120].replace("\n", " ")

    @staticmethod
    def _hash_id(file_path: str, line: int) -> str:
        raw = f"{file_path}:{line}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    # ── TF-IDF Search ────────────────────────────────────────────────────

    def _build_idf(self) -> None:
        """Build TF-IDF index from chunks."""
        import math
        doc_count = max(len(self._chunks), 1)
        token_df: dict[str, int] = {}
        self._chunk_tokens = {}

        for cid, chunk in self._chunks.items():
            text = f"{chunk.name} {chunk.summary} {chunk.content}"
            tokens = set(_tokenize(text))
            self._chunk_tokens[cid] = tokens
            for t in tokens:
                token_df[t] = token_df.get(t, 0) + 1

        min_df = max(1, int(doc_count * 0.005))
        self._idf = {
            t: math.log((doc_count + 1) / (df + 1)) + 1.0
            for t, df in token_df.items()
            if df >= min_df
        }

    def search(self, query: str, top_k: int = 10) -> list[SearchResult]:
        """Semantic search over the codebase. Returns ranked results."""
        if not self._indexed:
            self.index_project()
        if not self._chunks:
            return []

        query_tokens = set(_tokenize(query))
        if not query_tokens:
            return []

        query_lower = query.lower()

        # Try embedding search first if available
        if _check_st() and self._embedder is None:
            self._init_embedder()
        if self._embedder is not None and self._embeddings:
            return self._search_embedding(query, top_k)

        # TF-IDF fallback
        return self._search_tfidf(query_lower, query_tokens, top_k)

    def _search_tfidf(self, query_lower: str, query_tokens: set[str],
                       top_k: int) -> list[SearchResult]:
        """TF-IDF weighted token overlap search."""
        results: list[SearchResult] = []
        idf = self._idf

        for cid, chunk in self._chunks.items():
            score = 0.0
            # Name/summary matches get a boost
            if query_lower in chunk.name.lower():
                score += 10.0
            if query_lower in chunk.summary.lower():
                score += 5.0
            if query_lower in chunk.content.lower():
                score += 2.0

            # Token-level IDF-weighted overlap
            chunk_tokens = self._chunk_tokens.get(cid, set())
            for token in query_tokens:
                w = idf.get(token, 1.0)
                if token in chunk_tokens:
                    score += 2.0 * w

            if score > 0.5:
                results.append(SearchResult(chunk=chunk, score=score, match_type="tfidf"))

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]

    def _init_embedder(self) -> None:
        """Lazy-init the embedding model."""
        try:
            self._embedder = _SentenceTransformer("all-MiniLM-L6-v2")
            # Pre-compute embeddings for all chunks
            texts = [c.summary + "\n" + c.content[:500] for c in self._chunks.values()]
            if texts:
                vectors = self._embedder.encode(texts, show_progress_bar=False)
                for cid, vec in zip(self._chunks.keys(), vectors):
                    self._embeddings[cid] = vec.tolist()
            logger.info("RAG embeddings ready: %d chunks", len(self._embeddings))
        except Exception as e:
            logger.debug("Embedding init failed: %s", e)
            self._embedder = None

    def _search_embedding(self, query: str, top_k: int) -> list[SearchResult]:
        """Cosine similarity search using embeddings."""
        import math
        try:
            q_vec = self._embedder.encode([query], show_progress_bar=False)[0]
        except Exception:
            return self._search_tfidf(query.lower(), set(_tokenize(query)), top_k)

        results: list[SearchResult] = []
        for cid, chunk in self._chunks.items():
            c_vec = self._embeddings.get(cid)
            if c_vec is None:
                continue
            # Cosine similarity
            dot = sum(a * b for a, b in zip(q_vec, c_vec))
            q_norm = math.sqrt(sum(a * a for a in q_vec))
            c_norm = math.sqrt(sum(a * a for a in c_vec))
            sim = dot / (q_norm * c_norm + 1e-10)
            if sim > 0.3:
                results.append(SearchResult(chunk=chunk, score=float(sim), match_type="embedding"))

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]

    def get_context(self, query: str, top_k: int = 5,
                    max_chars: int = 6000) -> str:
        """Get formatted context from RAG search for inclusion in prompts.

        Args:
            query: Natural-language search query.
            top_k: Number of top results to include.
            max_chars: Maximum total characters in the output.

        Returns:
            Formatted string with file paths, line numbers, and content.
        """
        results = self.search(query, top_k=top_k)
        if not results:
            return ""

        lines: list[str] = ["\n## Codebase Context (RAG)"]
        total_chars = 0
        for i, sr in enumerate(results[:top_k], 1):
            c = sr.chunk
            header = (
                f"\n### [{i}] {c.file_path}:{c.start_line}-{c.end_line} "
                f"({c.kind}: {c.name}) [score: {sr.score:.2f}]"
            )
            # Truncate content to fit budget
            budget = max_chars - total_chars - len(header) - 50
            if budget <= 0:
                break
            content = c.content
            if len(content) > budget:
                content = content[:budget] + "\n... (truncated)"
            lines.append(header)
            lines.append(f"```\n{content}\n```")
            total_chars += len(header) + len(content) + 10

        return "\n".join(lines)

    def get_relevant_files(self, query: str, top_k: int = 3) -> list[str]:
        """Get just the file paths of the most relevant files."""
        results = self.search(query, top_k=top_k)
        seen: set[str] = set()
        files: list[str] = []
        for sr in results:
            fp = sr.chunk.file_path
            if fp not in seen:
                seen.add(fp)
                files.append(fp)
        return files[:top_k]


# ── Global singleton ─────────────────────────────────────────────────────────

_rag_index: RAGIndex | None = None


def get_rag_index(workspace_dir: str | Path = ".") -> RAGIndex:
    """Get or create the RAG index for a workspace."""
    global _rag_index
    ws = str(Path(workspace_dir).resolve())
    if _rag_index is None or str(_rag_index._workspace) != ws:
        _rag_index = RAGIndex(workspace_dir)
    return _rag_index
