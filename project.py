"""
Project auto-detection.

Scans the workspace for known project files and detects:
- Programming languages
- Frameworks and libraries
- Build systems / package managers
- Test frameworks
- Code style tools (linters, formatters)
- Git repository info

The detected info is injected into the agent's system prompt so the LLM
understands the project context from the start.
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ── Detection rules ──────────────────────────────────────────────────────────

# File patterns → language
LANGUAGE_DETECTORS: dict[str, list[str]] = {
    "Python": ["pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile", "poetry.lock"],
    "JavaScript": ["package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", ".nvmrc"],
    "TypeScript": ["tsconfig.json", "tsconfig.*.json"],
    "Go": ["go.mod", "go.sum"],
    "Rust": ["Cargo.toml", "Cargo.lock"],
    "Java": ["pom.xml", "build.gradle", "build.gradle.kts", "gradlew", ".java-version"],
    "Kotlin": ["build.gradle.kts", "settings.gradle.kts"],
    "Ruby": ["Gemfile", "Rakefile", ".ruby-version"],
    "PHP": ["composer.json", "composer.lock"],
    "C/C++": ["CMakeLists.txt", "Makefile", "configure.ac"],
    "C#": ["*.csproj", "*.sln", "global.json"],
    "Swift": ["Package.swift"],
    "Zig": ["build.zig"],
    "Elixir": ["mix.exs"],
    "Clojure": ["deps.edn", "project.clj"],
    "Haskell": ["stack.yaml", "*.cabal"],
    "Scala": ["build.sbt"],
    "Dart": ["pubspec.yaml"],
    "Lua": ["*.rockspec"],
}


# File patterns → build system
BUILD_DETECTORS: dict[str, list[str]] = {
    "pip": ["setup.py", "setup.cfg", "requirements.txt"],
    "poetry": ["poetry.lock", "pyproject.toml"],
    "npm": ["package-lock.json"],
    "yarn": ["yarn.lock"],
    "pnpm": ["pnpm-lock.yaml"],
    "cargo": ["Cargo.toml", "Cargo.lock"],
    "go mod": ["go.mod"],
    "gradle": ["build.gradle", "build.gradle.kts"],
    "maven": ["pom.xml"],
    "cmake": ["CMakeLists.txt"],
    "mix": ["mix.exs"],
    "stack": ["stack.yaml"],
    "cabal": ["*.cabal"],
}

# File patterns → test framework
TEST_DETECTORS: dict[str, list[str]] = {
    "pytest": ["pytest.ini", "pyproject.toml", "conftest.py"],
    "unittest": ["test_*.py", "*_test.py"],
    "jest": ["jest.config.js", "jest.config.ts"],
    "vitest": ["vitest.config.js", "vitest.config.ts"],
    "mocha": [".mocharc.js", ".mocharc.json"],
    "go test": ["*_test.go"],
    "JUnit": ["*Test.java", "*Tests.java"],
    "RSpec": ["spec/"],
    "PHPUnit": ["phpunit.xml"],
    "cargo test": [],
    "Catch2": ["catch.hpp", "catch2.hpp"],
}


# ── Project info ─────────────────────────────────────────────────────────────

@dataclass
class ProjectInfo:
    """Detected project information."""
    languages: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    build_systems: list[str] = field(default_factory=list)
    test_frameworks: list[str] = field(default_factory=list)
    is_git_repo: bool = False
    git_branch: str = ""
    git_remote: str = ""
    has_docker: bool = False
    has_docker_compose: bool = False
    has_ci_cd: bool = False
    ci_system: str = ""
    file_count: int = 0
    directory_count: int = 0

    def to_prompt(self) -> str:
        """Format as a system prompt section — actionable, not just informative."""
        lines = ["## Project Context"]

        # Stack summary (single line)
        parts = []
        if self.languages:
            parts.append(f"**Language:** {', '.join(self.languages)}")
        if self.build_systems:
            parts.append(f"**Build:** {', '.join(self.build_systems)}")
        if self.test_frameworks:
            parts.append(f"**Test:** {', '.join(self.test_frameworks)}")
        if parts:
            lines.append(" · ".join(parts))

        # Git context
        if self.is_git_repo:
            git_line = f"**Git:** branch `{self.git_branch}`"
            if self.git_remote:
                git_line += f" → `{self.git_remote}`"
            lines.append(git_line)

        # Running commands (test, build, lint) — most useful for the LLM
        commands = []
        if "pytest" in self.test_frameworks:
            commands.append(f"Test: `pytest tests/ -q`")
        if "jest" in self.test_frameworks:
            commands.append(f"Test: `npm test`")
        if "npm" in self.build_systems:
            commands.append(f"Build: `npm run build`")
        if "pip" in self.build_systems or "poetry" in self.build_systems:
            commands.append(f"Install: `pip install -e .`")
        if commands:
            lines.append("**Commands:** " + "  ".join(commands))

        # Size for context
        lines.append(f"**Size:** ~{self.file_count} files, {self.directory_count} dirs")

        # Project instructions file (CLAUDE.md, etc.)
        if self.project_rules_file:
            lines.append(f"\n### Project Instructions (`{self.project_rules_file}`)\n")
            lines.append(self._summarise_rules())

        # Code style sampling (if available)
        if self.code_style_notes:
            lines.append(f"\n### Code Conventions (sampled)\n{self.code_style_notes}")

        # Recent git activity
        if self.recent_commits:
            lines.append(f"\n### Recent Commits\n{self.recent_commits}")

        return "\n".join(lines)

    def _summarise_rules(self) -> str:
        """Extract the most important rules from a project instructions file.

        Returns the first ~2000 chars of non-YAML-frontmatter content,
        which covers the key rules without eating too many tokens.
        """
        if not self.project_rules_content:
            return ""
        # Strip YAML frontmatter
        content = self.project_rules_content
        if content.startswith("---"):
            parts = content.split("---", 2)
            content = parts[-1] if len(parts) >= 3 else content
        # Take first 2000 chars — enough for rules, not whole file
        if len(content) > 2000:
            content = content[:2000] + "\n... (truncated)"
        return content.strip()

    # Extended fields (filled by ProjectDetector)
    project_rules_file: str = ""
    project_rules_content: str = ""
    code_style_notes: str = ""
    recent_commits: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "languages": self.languages,
            "frameworks": self.frameworks,
            "build_systems": self.build_systems,
            "test_frameworks": self.test_frameworks,
            "is_git_repo": self.is_git_repo,
            "git_branch": self.git_branch,
            "git_remote": self.git_remote,
            "has_docker": self.has_docker,
            "has_docker_compose": self.has_docker_compose,
            "file_count": self.file_count,
            "directory_count": self.directory_count,
        }


# ── Detector ─────────────────────────────────────────────────────────────────

class ProjectDetector:
    """Scans a directory and detects project characteristics."""

    def __init__(self, project_dir: str | Path | None = None):
        self.root = Path(project_dir) if project_dir else Path.cwd()

    def detect(self) -> ProjectInfo:
        """Run all detectors and return a ProjectInfo."""
        info = ProjectInfo()

        # Scan files in root
        root_files = set()
        all_files: list[str] = []

        if self.root.exists():
            for entry in self.root.iterdir():
                if entry.is_file() and not entry.name.startswith("."):
                    root_files.add(entry.name)

            # Walk for deeper files (1-2 levels)
            for root, dirs, files in os.walk(self.root):
                dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__", ".git", "venv", ".venv")]
                if root.count(os.sep) - str(self.root).count(os.sep) > 2:
                    dirs[:] = []
                    continue
                for f in files:
                    all_files.append(os.path.relpath(os.path.join(root, f), self.root))

        info.file_count = len(all_files)
        info.directory_count = len(set(
            d for d in (os.path.dirname(f) for f in all_files) if d
        ))

        # Unified detection pass — run all detectors in one loop
        _DETECTOR_TARGETS: list[tuple[dict[str, list[str]], list[str]]] = [
            (LANGUAGE_DETECTORS, info.languages),
            (BUILD_DETECTORS, info.build_systems),
            (TEST_DETECTORS, info.test_frameworks),
        ]
        for detector_map, target_list in _DETECTOR_TARGETS:
            for name, indicators in detector_map.items():
                if any(self._match_indicator(ind, root_files, all_files)
                       for ind in indicators) and name not in target_list:
                    target_list.append(name)

        # Git detection
        git_dir = self.root / ".git"
        if git_dir.exists():
            info.is_git_repo = True
            info.git_branch = self._get_git_branch()
            info.git_remote = self._get_git_remote()

        # Docker detection
        if (self.root / "Dockerfile").exists():
            info.has_docker = True
        compose_files = list(self.root.glob("docker-compose*.yml")) + list(self.root.glob("docker-compose*.yaml"))
        if compose_files:
            info.has_docker_compose = True

        # CI/CD detection
        ci_indicators = {
            ".github/workflows": "GitHub Actions",
            ".gitlab-ci.yml": "GitLab CI",
            "Jenkinsfile": "Jenkins",
            ".circleci": "CircleCI",
            ".travis.yml": "Travis CI",
            "azure-pipelines.yml": "Azure Pipelines",
            "buildkite": "Buildkite",
        }
        for indicator, name in ci_indicators.items():
            if (self.root / indicator).exists() or any(indicator in f for f in all_files):
                info.has_ci_cd = True
                info.ci_system = name
                break

        # Auto-read project instructions file
        info.project_rules_file, info.project_rules_content = self._read_project_rules()

        # Sample code style from source files
        info.code_style_notes = self._sample_code_style(info.languages[:1] if info.languages else [])

        # Recent git activity
        info.recent_commits = self._get_recent_commits()

        logger.info(
            "Project detected: langs=%s, frameworks=%s, build=%s, tests=%s",
            info.languages, info.frameworks, info.build_systems, info.test_frameworks,
        )
        return info

    def _read_project_rules(self) -> tuple[str, str]:
        """Auto-read CLAUDE.md, CONTRIBUTING.md, or similar project instructions."""
        candidates = ["CLAUDE.md", "CONTRIBUTING.md", "DEVELOPMENT.md", "CODING.md", "RULES.md", ".cursorrules"]
        for name in candidates:
            path = self.root / name
            if path.exists():
                try:
                    return name, path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    return "", ""
        return "", ""

    def _sample_code_style(self, languages: list[str]) -> str:
        """Sample a few source files to detect naming and formatting conventions."""
        if not languages:
            return ""
        ext_map = {"Python": ".py", "JavaScript": ".js", "TypeScript": ".ts",
                    "Go": ".go", "Rust": ".rs", "Java": ".java", "Ruby": ".rb",
                    "C/C++": ".cpp", "PHP": ".php", "C#": ".cs", "Kotlin": ".kt"}
        ext = ext_map.get(languages[0], "")
        if not ext:
            return ""

        # Find up to 3 source files to sample
        samples = []
        for root, dirs, files in os.walk(self.root):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__", ".git", "venv", ".venv", "dist", "build")]
            for f in files:
                if f.endswith(ext) and len(samples) < 3:
                    samples.append(os.path.join(root, f))
            if len(samples) >= 3:
                break

        if not samples:
            return ""

        notes: list[str] = []
        for fp in samples[:3]:
            try:
                content = Path(fp).read_text(encoding="utf-8", errors="replace")
                lines = content.splitlines()
                # Detect indentation
                spaces = sum(1 for l in lines if l.startswith("    "))
                tabs = sum(1 for l in lines if l.startswith("\t"))
                indent = "4-space" if spaces > tabs else "tabs" if tabs > spaces else "2-space"
                # Detect quote style
                single = content.count("'")
                double = content.count('"')
                quotes = "single-quotes" if single > double * 2 else "double-quotes"
                notes.append(f"- `{os.path.basename(fp)}`: {indent} indentation, {quotes}")
            except OSError:
                pass

        return "\n".join(notes) if notes else ""

    def _get_recent_commits(self) -> str:
        """Get last 5 commit messages for context."""
        import subprocess
        try:
            result = subprocess.run(
                ["git", "log", "--oneline", "-5", "--no-decorate"],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                cwd=str(self.root), timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _match_indicator(self, pattern: str, root_files: set, all_files: list[str]) -> bool:
        """Check if a file indicator exists."""
        if "*" in pattern:
            # Glob pattern
            import fnmatch
            return any(fnmatch.fnmatch(os.path.basename(f), pattern) for f in all_files)
        if pattern in root_files:
            return True
        # Check deeper files
        for f in all_files:
            if os.path.basename(f) == pattern:
                return True
            if f.endswith(os.sep + pattern):
                return True
        return False

    def _get_git_branch(self) -> str:
        """Get current git branch."""
        import subprocess
        try:
            result = subprocess.run(
                ["git", "branch", "--show-current"],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                cwd=str(self.root), timeout=5,
            )
            return result.stdout.strip()
        except Exception:  # noqa: BLE001
            return ""

    def _get_git_remote(self) -> str:
        """Get git remote URL."""
        import subprocess
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                cwd=str(self.root), timeout=5,
            )
            return result.stdout.strip()
        except Exception:  # noqa: BLE001
            return ""
