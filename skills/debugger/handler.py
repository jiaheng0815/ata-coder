"""
Debugger skill handler — pre-scans for common bug patterns before the LLM
starts debugging, saving discovery turns.

Runs fast local scans and injects findings into the skill prompt as
"preliminary scan results" so the agent can jump straight to deep analysis.
"""
import re
from pathlib import Path
from typing import Any

# Patterns that commonly indicate bugs (fast regex scan, no AST needed)
BUG_PATTERNS = [
    (r'except\s*:', "Bare except clause (swallows KeyboardInterrupt/SystemExit)"),
    (r'except\s+Exception\s*:\s*pass\b', "Exception swallowed silently"),
    (r'\.get\([^)]+\)\s*\[', "dict.get() result used without None check"),
    (r'=\s*\[\]\s*\n.*\.append', "Mutable default argument (list) — shared across calls"),
    (r'=\s*\{\}\s*\n.*\.update', "Mutable default argument (dict) — shared across calls"),
    (r'os\.system\(', "os.system() — use subprocess.run() instead"),
    (r'subprocess\.call\(', "subprocess.call() — use subprocess.run() instead"),
    (r'import\s+asyncio\b.*\n.*asyncio\.run\(', "asyncio.run() inside async context possible"),
    (r'__del__.*import\s+', "import inside __del__ — will fail during interpreter shutdown"),
    (r'f""".*\{.*\}.*"""', "f-string inside docstring — will fail at definition time"),
]


def run(input_data: dict[str, Any]) -> dict[str, Any]:
    """Scan mentioned files for common bug patterns.

    Expected input keys:
        task: str           — the user's debugging request
        workspace: str      — absolute path to project root
        mentioned_files: list[str] — files the user referenced (optional)

    Returns:
        dict with 'findings' list and 'scan_summary' string.
    """
    workspace = Path(input_data.get("workspace", ".")).resolve()
    mentioned = input_data.get("mentioned_files", [])
    task = input_data.get("task", "")

    # Extract file paths from task if not provided
    if not mentioned:
        mentioned = _extract_files_from_task(task)

    findings: list[dict] = []
    for rel_path in mentioned[:15]:
        full_path = workspace / rel_path
        if not full_path.is_file():
            continue
        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        for lineno, line in enumerate(content.splitlines(), 1):
            for pattern, description in BUG_PATTERNS:
                if re.search(pattern, line):
                    findings.append({
                        "file": rel_path,
                        "line": lineno,
                        "code": line.strip()[:120],
                        "issue": description,
                    })
                    break  # one finding per line

    summary = ""
    if findings:
        files = sorted(set(f["file"] for f in findings))
        summary = (f"Pre-scan found {len(findings)} potential issues "
                   f"in {len(files)} file(s): "
                   + ", ".join(f"{f['file']}:{f['line']}" for f in findings[:5])
                   + ("..." if len(findings) > 5 else ""))

    return {
        "findings": findings[:30],
        "scan_summary": summary,
    }


def _extract_files_from_task(task: str) -> list[str]:
    """Extract file paths from task text."""
    pattern = r'(?:^|\s|["\x60])(\.?/?[a-zA-Z0-9_\-./]+\.(?:py|js|ts|rs|go|java|toml|yaml|json|md|sh|sql|html|css))(?:\s|$|["\x60:])'  # noqa: E501
    return list(dict.fromkeys(re.findall(pattern, task)))
