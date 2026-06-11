"""
Central settings management for ATA Coder.

Stores all persistent configuration in ~/.ata_coder/settings.json:
- Model mapping (opus/sonnet/haiku/subagent → actual provider model)
- Complexity detection rules (simple vs complex task routing)
- Storage paths (skills, sessions, memory, changes)

Usage:
    from .settings import get_settings
    s = get_settings()
    model = s.model_for(task)  # auto-route based on complexity
"""

import json
import logging
import os
import shutil
import re
import sysconfig
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Default settings ──────────────────────────────────────────────────────────

DEFAULT_SETTINGS: dict[str, Any] = {
    "api": {
        "base_url": "https://api.deepseek.com",
        "api_key": "",
    },
    "model": {
        "default": "deepseek-v4-pro",
        "mapping": {
            "opus": "deepseek-v4-pro",
            "sonnet": "deepseek-v4-pro",
            "haiku": "deepseek-v4-flash",
            "subagent": "deepseek-v4-flash",
        },
    },
    "complexity": {
        "auto_detect": True,
        # Thresholds: skip AI classify for obvious cases
        "simple_max_chars": 60,    # very short → assume simple
        "complex_min_chars": 500,  # very long → assume complex
    },
    "paths": {
        "data": "~/.ata_coder",
        "skills": "~/.ata_coder/skills",
        "sessions": "~/.ata_coder/sessions",
        "memory": "~/.ata_coder/memory",
        "changes": "~/.ata_coder/changes",
    },
}


# ── Settings class ────────────────────────────────────────────────────────────

@dataclass
class Settings:
    """Singleton settings manager backed by ~/.ata_coder/settings.json."""

    _data: dict[str, Any] = field(default_factory=dict)
    _file: Path | None = None

    # ── Init / Load / Save ──────────────────────────────────────────────────

    def load(self, file_path: str | Path | None = None) -> "Settings":
        """Load settings from disk, creating defaults if needed."""
        if file_path:
            self._file = Path(file_path)
        else:
            self._file = Path.home() / ".ata_coder" / "settings.json"

        self._file.parent.mkdir(parents=True, exist_ok=True)

        if self._file.exists():
            try:
                with open(self._file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                self._data = self._deep_merge(DEFAULT_SETTINGS, loaded)
                logger.debug("Loaded settings from %s", self._file)
            except Exception as e:
                logger.warning("Failed to load %s, using defaults: %s", self._file, e)
                self._data = dict(DEFAULT_SETTINGS)
                self.save()
        else:
            self._data = dict(DEFAULT_SETTINGS)
            self.save()
            logger.info("Created default settings at %s", self._file)

        return self

    def save(self) -> None:
        """Persist current settings to disk."""
        if not self._file:
            return
        try:
            with open(self._file, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error("Failed to save settings: %s", e)

    def reload(self) -> None:
        """Reload settings from disk."""
        self.load(self._file)

    # ── Access ──────────────────────────────────────────────────────────────

    def get(self, *keys: str, default: Any = None) -> Any:
        """Get a nested key, e.g. settings.get('model', 'default')."""
        node = self._data
        for k in keys:
            if isinstance(node, dict):
                node = node.get(k)
            else:
                return default
            if node is None:
                return default
        return node

    def set(self, *keys: str, value: Any, save: bool = True) -> None:
        """Set a nested key, e.g. settings.set('model', 'default', 'gpt-4o')."""
        node = self._data
        for k in keys[:-1]:
            if k not in node:
                node[k] = {}
            node = node[k]
        node[keys[-1]] = value
        if save:
            self.save()

    @property
    def data(self) -> dict:
        return self._data

    # ── Path helpers ────────────────────────────────────────────────────────

    def resolve_path(self, key: str) -> Path:
        """Resolve a path from settings (expanding ~)."""
        raw = self.get("paths", key, default=f"~/.ata_coder/{key}")
        return Path(raw).expanduser().resolve()

    @property
    def api_base_url(self) -> str:
        return self.get("api", "base_url", default="https://api.deepseek.com")

    @property
    def api_key(self) -> str:
        return self.get("api", "api_key", default="")

    @property
    def data_dir(self) -> Path:
        return self.resolve_path("data")

    @property
    def skills_dir(self) -> Path:
        return self.resolve_path("skills")

    @property
    def sessions_dir(self) -> Path:
        return self.resolve_path("sessions")

    @property
    def memory_dir(self) -> Path:
        return self.resolve_path("memory")

    @property
    def changes_dir(self) -> Path:
        return self.resolve_path("changes")

    def ensure_dirs(self) -> None:
        """Create all configured directories."""
        for key in ("data", "skills", "sessions", "memory", "changes"):
            d = self.resolve_path(key)
            d.mkdir(parents=True, exist_ok=True)

    # ── Model routing ───────────────────────────────────────────────────────

    @property
    def default_model(self) -> str:
        return self.get("model", "default", default="deepseek-v4-pro")

    @property
    def model_opus(self) -> str:
        return self.get("model", "mapping", "opus", default=self.default_model)

    @property
    def model_sonnet(self) -> str:
        return self.get("model", "mapping", "sonnet", default=self.default_model)

    @property
    def model_haiku(self) -> str:
        return self.get("model", "mapping", "haiku", default=self.default_model)

    @property
    def model_subagent(self) -> str:
        return self.get("model", "mapping", "subagent", default=self.default_model)

    def shortcut_classify(self, task: str) -> str | None:
        """
        Quick length-based shortcut — skip AI classify for obvious cases.
        Returns 'simple', 'complex', or None (None = need AI classify).
        """
        if not self.get("complexity", "auto_detect", default=True):
            return "normal"

        task_len = len(task.strip())
        simple_max = self.get("complexity", "simple_max_chars", default=60)
        complex_min = self.get("complexity", "complex_min_chars", default=500)

        if task_len <= simple_max:
            return "simple"
        if task_len >= complex_min:
            return "complex"
        return None  # middle ground → let AI decide

    # ── Internal ────────────────────────────────────────────────────────────

    @staticmethod
    def _deep_merge(base: dict, override: dict) -> dict:
        """Recursively merge override into base."""
        result = dict(base)
        for k, v in override.items():
            if k in result and isinstance(result[k], dict) and isinstance(v, dict):
                result[k] = Settings._deep_merge(result[k], v)
            else:
                result[k] = v
        return result


# ── Global singleton ──────────────────────────────────────────────────────────

_settings: Settings | None = None


def get_settings(file_path: str | Path | None = None) -> Settings:
    """Get or create the global Settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings().load(file_path)
    return _settings


def init_settings(file_path: str | Path | None = None) -> Settings:
    """Initialize settings, seeding skills from project source if needed."""
    settings = get_settings(file_path)

    # Ensure all directories exist
    settings.ensure_dirs()

    # Seed default skills from project source → ~/.ata_coder/skills/
    _seed_skills(settings)

    # Seed default memories from project source → ~/.ata_coder/memory/
    _seed_memories(settings)

    return settings


def _find_source_dir(name: str) -> Path | None:
    """Find a data directory (skills, memory, prompts) in various locations."""
    # 1. Development: next to this file (project root)
    candidate = Path(__file__).parent / name
    if candidate.is_dir():
        return candidate
    # 2. pip install: site-packages data_files
    data_path = Path(sysconfig.get_path("data"))
    candidate = data_path / name
    if candidate.is_dir():
        return candidate
    # 3. pip install (user): user site data
    user_data = Path(sysconfig.get_path("data", scheme="nt_user" if os.name == "nt" else "posix_user"))
    candidate = user_data / name
    if candidate.is_dir():
        return candidate
    return None


def _seed_skills(settings: Settings) -> None:
    """Copy default skill files from project source to ~/.ata_coder/skills/ if empty."""
    target = settings.skills_dir
    if list(target.glob("*.md")):
        return  # already has skills

    source = _find_source_dir("skills")
    if not source:
        logger.debug("No skills source dir found, skipping seed")
        return

    target.mkdir(parents=True, exist_ok=True)
    for fp in source.glob("*.md"):
        shutil.copy2(fp, target / fp.name)
        logger.info("Seeded skill: %s → %s", fp.name, target / fp.name)
    for fp in source.glob("*.json"):
        shutil.copy2(fp, target / fp.name)


def _seed_memories(settings: Settings) -> None:
    """Copy default memory files from project source to ~/.ata_coder/memory/ if empty."""
    target = settings.memory_dir
    if list(target.glob("*.md")):
        return  # already has memories

    source = _find_source_dir("memory")
    if not source:
        logger.debug("No memory source dir found, skipping seed")
        return

    target.mkdir(parents=True, exist_ok=True)
    for fp in source.glob("*"):
        if fp.name == "__pycache__":
            continue
        if fp.is_file():
            shutil.copy2(fp, target / fp.name)
            logger.info("Seeded memory: %s → %s", fp.name, target / fp.name)
