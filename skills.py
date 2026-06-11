"""
Skills system — loads SKILL.md files (YAML frontmatter + markdown body).

Each skill is a .md file in the skills/ directory:

    ---
    name: debugger
    description: Diagnoses and fixes bugs.
    triggers:
      - debug
      - bug
      - 报错
    tools: []  # empty = all tools
    ---

    You are an expert debugger...

Supports:
- SKILL.md: markdown with YAML frontmatter (primary format)
- .json:  JSON format (legacy compatibility)
- .yaml/.yml: pure YAML format (legacy compatibility)
"""

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


# ── Skill data model ───────────────────────────────────────────────────────────

@dataclass
class Skill:
    """A named skill (persona) for the coding agent."""

    name: str
    description: str
    system_prompt: str
    tools: list[str] = field(default_factory=list)  # empty = all tools
    model: str | None = None
    temperature: float | None = None
    triggers: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_frontmatter(self) -> str:
        """Export as SKILL.md format."""
        d = {
            "name": self.name,
            "description": self.description,
            "triggers": self.triggers,
        }
        if self.tools:
            d["tools"] = self.tools
        if self.model:
            d["model"] = self.model
        if self.metadata:
            d["metadata"] = self.metadata
        fm = yaml.dump(d, default_flow_style=False, allow_unicode=True, sort_keys=False) if HAS_YAML else json.dumps(d, indent=2, ensure_ascii=False)
        return f"---\n{fm}---\n\n{self.system_prompt}"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Skill":
        return cls(
            name=d["name"],
            description=d.get("description", ""),
            system_prompt=d.get("system_prompt", ""),
            tools=d.get("tools", []),
            model=d.get("model"),
            temperature=d.get("temperature"),
            triggers=d.get("triggers", []),
            metadata=d.get("metadata", {}),
        )

    @classmethod
    def from_frontmatter(cls, raw: str, source: str = "unknown") -> "Skill | None":
        """Parse a SKILL.md file (YAML frontmatter + markdown body)."""
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", raw, re.DOTALL)
        if not match:
            logger.warning("No YAML frontmatter in %s", source)
            return None
        try:
            if HAS_YAML:
                meta = yaml.safe_load(match.group(1))
            else:
                meta = json.loads(match.group(1))
        except Exception as e:
            logger.warning("Failed to parse frontmatter in %s: %s", source, e)
            return None
        if not isinstance(meta, dict):
            return None
        return cls(
            name=meta.get("name", Path(source).stem),
            description=meta.get("description", ""),
            system_prompt=match.group(2).strip(),
            tools=meta.get("tools", []),
            model=meta.get("model"),
            temperature=meta.get("temperature"),
            triggers=meta.get("triggers", []),
            metadata=meta.get("metadata", {}),
        )


# ── Skill manager ─────────────────────────────────────────────────────────────

class SkillManager:
    """Loads skills from SKILL.md files, matches by trigger keywords."""

    def __init__(self, skills_dir: str | Path | None = None):
        if skills_dir is None:
            try:
                from .settings import get_settings, init_settings
                s = init_settings()  # seeds default skills if needed
                skills_dir = s.skills_dir
            except Exception:
                skills_dir = Path.home() / ".ata_coder" / "skills"
        self.skills_dir = Path(skills_dir)
        self.skills_dir.mkdir(parents=True, exist_ok=True)

        self._skills: dict[str, Skill] = {}
        self._active_skill: Skill | None = None

        self._load_from_directory()

    # ── Loading ─────────────────────────────────────────────────────────────

    def _load_from_directory(self) -> None:
        """Load skills from SKILL.md files (primary), plus JSON/YAML (legacy)."""
        if not self.skills_dir.exists():
            return

        # SKILL.md files (primary format)
        for fp in sorted(self.skills_dir.glob("*.md")):
            try:
                raw = fp.read_text(encoding="utf-8")
                skill = Skill.from_frontmatter(raw, source=fp.name)
                if skill:
                    self._skills[skill.name] = skill
                    logger.debug("Loaded skill: %s from %s", skill.name, fp.name)
            except Exception as e:
                logger.warning("Failed to load %s: %s", fp.name, e)

        # Legacy JSON files
        for fp in self.skills_dir.glob("*.json"):
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                items = data if isinstance(data, list) else [data]
                for item in items:
                    skill = Skill.from_dict(item)
                    self._skills[skill.name] = skill
                    logger.debug("Loaded skill (json): %s from %s", skill.name, fp.name)
            except Exception as e:
                logger.warning("Failed to load %s: %s", fp.name, e)

        # Legacy YAML files
        if HAS_YAML:
            for fp in list(self.skills_dir.glob("*.yaml")) + list(self.skills_dir.glob("*.yml")):
                try:
                    data = yaml.safe_load(fp.read_text(encoding="utf-8"))
                    items = data if isinstance(data, list) else [data]
                    for item in items:
                        skill = Skill.from_dict(item)
                        self._skills[skill.name] = skill
                        logger.debug("Loaded skill (yaml): %s from %s", skill.name, fp.name)
                except Exception as e:
                    logger.warning("Failed to load %s: %s", fp.name, e)

        logger.debug("Loaded %d skills total", len(self._skills))

    # ── Management ──────────────────────────────────────────────────────────

    def list_skills(self) -> list[Skill]:
        return list(self._skills.values())

    def get_skill(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def activate(self, name: str) -> Skill | None:
        skill = self._skills.get(name)
        if skill:
            self._active_skill = skill
            logger.info("Activated: %s", skill.name)
        else:
            logger.warning("Skill not found: %s", name)
        return skill

    def deactivate(self) -> None:
        self._active_skill = None
        logger.info("Skill deactivated")

    @property
    def active_skill(self) -> Skill | None:
        return self._active_skill

    def get_system_prompt(self) -> str:
        if self._active_skill:
            return self._active_skill.system_prompt
        default = self._skills.get("general-coder")
        return default.system_prompt if default else "You are an expert coding assistant."

    def get_allowed_tools(self) -> list[str] | None:
        if self._active_skill and self._active_skill.tools:
            return self._active_skill.tools
        return None

    # ── Detection ───────────────────────────────────────────────────────────

    def detect_skill(self, user_input: str) -> Skill | None:
        """
        Auto-detect skill from trigger keywords.
        Multi-word triggers match if ALL words appear in the input.
        More specific skills win over general-coder.
        """
        user_lower = user_input.lower()

        def trigger_matches(trigger: str, text: str) -> bool:
            t = trigger.lower()
            words = t.split()
            if len(words) > 1:
                return all(w in text for w in words)
            return t in text

        candidates: list[tuple[int, Skill]] = []
        for skill in self._skills.values():
            if not skill.triggers:
                continue
            score = sum(len(t.split()) for t in skill.triggers if trigger_matches(t, user_lower))
            if score > 0:
                candidates.append((score, skill))

        if not candidates:
            return None

        # Sort: higher score first; break ties against general-coder
        candidates.sort(key=lambda x: (-x[0], 1 if x[1].name == "general-coder" else 0))
        best = candidates[0][1]

        if best.name == "general-coder" and len(candidates) > 1:
            for _, skill in candidates[1:]:
                if skill.name != "general-coder":
                    best = skill
                    break

        logger.debug("Detected: %s (%d candidates)", best.name, len(candidates))
        return best

    # ── Persistence ─────────────────────────────────────────────────────────

    def save_skill(self, skill: Skill) -> Path:
        """Save a skill as SKILL.md file."""
        fp = self.skills_dir / f"{skill.name}.md"
        fp.write_text(skill.to_frontmatter(), encoding="utf-8")
        self._skills[skill.name] = skill
        logger.info("Saved: %s → %s", skill.name, fp.name)
        return fp

    def delete_skill(self, name: str) -> bool:
        """Delete a skill file (any format)."""
        for ext in (".md", ".json", ".yaml", ".yml"):
            fp = self.skills_dir / f"{name}{ext}"
            if fp.exists():
                fp.unlink()
                self._skills.pop(name, None)
                logger.info("Deleted: %s", name)
                return True
        return False


# ── Global ─────────────────────────────────────────────────────────────────────

_skill_manager: SkillManager | None = None


def get_skill_manager(skills_dir: str | None = None) -> SkillManager:
    global _skill_manager
    if _skill_manager is None:
        _skill_manager = SkillManager(skills_dir)
    return _skill_manager
