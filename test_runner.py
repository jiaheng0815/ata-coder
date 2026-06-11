"""
Test runner + auto-fix loop. Detects test framework, runs tests,
parses failures, feeds errors back to agent for automatic fixing.

Supports: pytest, unittest, jest, vitest, mocha, go test, cargo test, phpunit, rspec.

Commands (added to registry):
  /test          — Run tests in current project
  /test-fix      — Run tests, auto-fix failures up to 3 times
"""

import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Test framework detection
# ═══════════════════════════════════════════════════════════════════════════════

FRAMEWORK_DETECTORS = [
    ("pytest", ["pytest.ini", "pyproject.toml", "conftest.py", "tox.ini"],
     "python -m pytest -v --tb=short 2>&1"),
    ("unittest", ["test_*.py", "*_test.py"],
     "python -m unittest discover -v 2>&1"),
    ("jest", ["jest.config.js", "jest.config.ts", "jest.config.mjs"],
     "npx jest --verbose 2>&1"),
    ("vitest", ["vitest.config.js", "vitest.config.ts"],
     "npx vitest --reporter verbose 2>&1"),
    ("mocha", [".mocharc.js", ".mocharc.json", ".mocharc.yml"],
     "npx mocha --reporter spec 2>&1"),
    ("go test", ["go.mod"], "go test ./... -v 2>&1"),
    ("cargo test", ["Cargo.toml"], "cargo test 2>&1"),
    ("phpunit", ["phpunit.xml", "phpunit.xml.dist"], "phpunit 2>&1"),
    ("rspec", ["spec/", ".rspec"], "bundle exec rspec 2>&1"),
]


def detect_framework(workspace: str | Path) -> tuple[str, str] | None:
    """Detect test framework and return (name, command). Only scans 2 levels deep."""
    root = Path(workspace)
    all_files = set()
    # Only scan root + 2 levels deep to avoid slow rglob in large projects
    for depth in range(3):
        pattern = "*/" * depth + "*"
        for entry in root.glob(pattern):
            if entry.is_file():
                name = entry.name
                rel = str(entry.relative_to(root))
                all_files.add(name)
                all_files.add(rel)

    for name, indicators, cmd in FRAMEWORK_DETECTORS:
        for ind in indicators:
            if "*" in ind:
                import fnmatch
                if any(fnmatch.fnmatch(f, ind) for f in all_files):
                    return name, cmd
            elif ind in all_files or any(ind in f for f in all_files):
                return name, cmd
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Test result parsing
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class TestResult:
    framework: str
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    duration: float = 0.0
    output: str = ""
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.errors == 0


def parse_results(framework: str, output: str) -> TestResult:
    """Parse test output into structured result."""
    result = TestResult(framework=framework, output=output)

    if framework == "pytest":
        m = re.search(r"(\d+) passed", output)
        if m: result.passed = int(m.group(1))
        m = re.search(r"(\d+) failed", output)
        if m: result.failed = int(m.group(1))
        m = re.search(r"(\d+) error", output)
        if m: result.errors = int(m.group(1))
        # Extract failure blocks
        failures = re.findall(r"FAILED.*?\n(.*?)(?:\n_+|\n=+|\Z)", output, re.DOTALL)
        result.failures = [f.strip()[:500] for f in failures]

    elif framework in ("jest", "vitest", "mocha"):
        m = re.search(r"Tests:\s+(\d+) passed.*?(\d+) failed.*?(\d+) total", output, re.DOTALL)
        if m: result.passed, result.failed = int(m.group(1)), int(m.group(2))
        failures = re.findall(r"●.*?\n(.*?)(?:\n\n|\Z)", output, re.DOTALL)
        result.failures = [f.strip()[:500] for f in failures]

    elif framework == "go test":
        result.failed = output.count("FAIL")
        result.passed = output.count("PASS") - output.count("FAIL")
        failures = re.findall(r"--- FAIL.*?\n(.*?)(?:\n---|\Z)", output, re.DOTALL)
        result.failures = [f.strip()[:500] for f in failures]

    elif framework == "cargo test":
        m = re.search(r"test result:.*?(\d+) passed.*?(\d+) failed", output)
        if m: result.passed, result.failed = int(m.group(1)), int(m.group(2))
        failures = re.findall(r"thread '.*?' panicked.*?:\n(.*)", output)
        result.failures = [f.strip()[:500] for f in failures]

    else:
        # Generic: count "FAIL" and "PASS" lines
        result.failed = output.count("FAIL")
        result.passed = max(0, output.count("ok") - output.count("not ok"))

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════════

def run_tests(workspace: str | Path, command: str | None = None) -> TestResult | None:
    """Run tests and return parsed result."""
    root = Path(workspace)
    if not command:
        detected = detect_framework(root)
        if not detected:
            return None
        _, command = detected

    logger.info("Running: %s", command)
    start = time.time()
    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=120, cwd=str(root),
        )
        output = proc.stdout + "\n" + proc.stderr
    except subprocess.TimeoutExpired:
        output = "Test run timed out after 120s"
    except Exception as e:
        output = str(e)

    elapsed = time.time() - start

    # Detect framework from output
    fw = "pytest" if "pytest" in command else (
        "jest" if "jest" in command else (
            "go test" if "go test" in command else "generic"
        )
    )
    result = parse_results(fw, output)
    result.duration = elapsed
    if not result.framework:
        result.framework = fw
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# Auto-fix loop
# ═══════════════════════════════════════════════════════════════════════════════

def auto_fix_loop(
    workspace: str | Path,
    agent,
    max_retries: int = 3,
    command: str | None = None,
) -> tuple[bool, str]:
    """
    Run tests → feed failures to agent → fix → repeat until pass or max retries.
    Returns (passed, summary).
    """
    results = []
    for attempt in range(max_retries):
        print(f"\n[Test attempt {attempt + 1}/{max_retries}]")
        result = run_tests(workspace, command)
        if result is None:
            return False, "No test framework detected."

        results.append(result)

        if result.ok:
            return True, f"All {result.passed} tests passed in {result.duration:.1f}s"

        if attempt == max_retries - 1:
            break

        # Feed failures to agent
        failure_text = "\n\n".join(result.failures[:3])
        if not failure_text:
            failure_text = result.output[-1000:]

        task = (
            f"The tests failed. Here are the failures:\n\n"
            f"```\n{failure_text}\n```\n\n"
            f"Read the relevant source files, fix the issues, and make the tests pass. "
            f"Be minimal — only fix what's broken."
        )
        print(f"  Failures: {result.failed} failed, {result.errors} errors")
        print(f"  Asking agent to fix...")
        agent.run(task, stream=True)

    # All retries exhausted
    summary = f"Failed after {max_retries} attempts. Last: {results[-1].failed} failures, {results[-1].errors} errors."
    return False, summary
