"""
Codecraft skill handler — preprocesses the task context before LLM prompt injection.

Gathers project metadata (language, framework, file tree), identifies relevant
source files, and enriches the skill prompt with concrete context so the LLM
doesn't waste turns on discovery.
"""
import json
from pathlib import Path
from typing import Any


def run(input_data: dict[str, Any]) -> dict[str, Any]:
    """Enrich the skill input with project context.

    Expected input keys:
        task: str          — the user's request
        workspace: str     — absolute path to the project root
        messages: list     — current conversation messages (optional)

    Returns:
        dict with enriched 'context' key merged into the skill prompt.
    """
    workspace = Path(input_data.get("workspace", ".")).resolve()
    task = input_data.get("task", "")

    context_parts: list[str] = []

    # ── 1. Detect project type from marker files ──────────────────────
    markers = _detect_project_markers(workspace)
    if markers:
        context_parts.append(f"Project type: {', '.join(markers)}")

    # ── 2. Extract file paths mentioned in the task ──────────────────
    mentioned_files = _extract_mentioned_files(task, workspace)
    if mentioned_files:
        context_parts.append("Files referenced in task:")
        for fp in mentioned_files[:10]:
            exists = " (exists)" if Path(fp).exists() else " (not found)"
            context_parts.append(f"  - {fp}{exists}")

    # ── 3. Detect git status (modified files, branch) ────────────────
    git_info = _detect_git_context(workspace)
    if git_info:
        context_parts.append(f"Git: {git_info}")

    return {
        "context": "\n".join(context_parts) if context_parts else "",
        "markers": markers,
        "mentioned_files": mentioned_files,
    }


def _detect_project_markers(root: Path) -> list[str]:
    """Detect language/framework from well-known marker files (fast, no deps)."""
    markers = []
    checks = [
        ("pyproject.toml", "Python"),
        ("package.json", "Node.js"),
        ("Cargo.toml", "Rust"),
        ("go.mod", "Go"),
        ("pom.xml", "Java/Maven"),
        ("build.gradle", "Java/Gradle"),
        ("Makefile", "C/Make"),
        ("CMakeLists.txt", "C++/CMake"),
        ("Dockerfile", "Docker"),
        (".github/workflows", "GitHub Actions"),
    ]
    for rel, label in checks:
        if (root / rel).exists():
            markers.append(label)
    # Detect Python specifically by .py files at root
    if not markers and list(root.glob("*.py")):
        markers.append("Python (no build tool)")
    return markers


def _extract_mentioned_files(task: str, workspace: Path) -> list[str]:
    """Extract file paths mentioned in the task string."""
    import re
    # Match patterns like "src/foo.py", "tests/test_bar.py", "./config.toml"
    pattern = r'(?:^|\s|["\x60])(\.?/?[a-zA-Z0-9_\-./]+\.(?:py|js|ts|rs|go|java|toml|yaml|json|md|sh|sql|html|css))(?:\s|$|["\x60:])'  # noqa: E501
    matches = re.findall(pattern, task)
    return list(dict.fromkeys(matches))[:20]  # dedup, preserve order


def _detect_git_context(root: Path) -> str:
    """Get brief git context without shelling out if possible."""
    git_dir = root / ".git"
    if not git_dir.exists():
        return ""
    head_file = git_dir / "HEAD"
    if head_file.exists():
        try:
            head = head_file.read_text(encoding="utf-8").strip()
            if head.startswith("ref: refs/heads/"):
                branch = head.replace("ref: refs/heads/", "")
                return f"branch={branch}"
        except Exception:
            pass
    return ""
