# -*- coding: utf-8 -*-
"""
Skills system — folder-based with SKILL.md manifest.

Each skill lives in its own folder under skills/:
    skills/
      skill-name/
        SKILL.md           # REQUIRED: identity, I/O schema, permissions, prompt
        handler.py         # optional: run(input_data) entry point
        prompts/           # optional: LLM prompt templates
        resources/         # optional: static data (tables, configs)
        tests/             # optional: test code
        requirements.txt   # optional: external dependencies
        README.md          # optional: developer/user docs

Backward-compatible: flat .md files still work (loaded as simple skills).

Design principles:
  - Single responsibility per skill
  - Explicit I/O contract (call.parameters → output.schema)
  - Self-contained context (no implicit conversation dependency)
  - Observable execution (logs, error codes, status)
  - Permission boundaries (network, filesystem, commands, domains)
"""

import importlib.util
import json
import logging
import re
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .utils import try_import_yaml

logger = logging.getLogger(__name__)

yaml, HAS_YAML = try_import_yaml()

__all__ = ["Skill", "SkillCallSpec", "SkillOutputSpec", "SkillPermissions",
           "SkillManager", "get_skill_manager"]


# ═══════════════════════════════════════════════════════════════════════════════
# I/O contract types
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class SkillCallSpec:
    """How to invoke this skill."""
    function: str = ""               # function name
    parameters: dict[str, Any] = field(default_factory=dict)
    # parameters: {name: {type, description, required, default}}


@dataclass
class SkillOutputSpec:
    """What this skill returns."""
    format: str = "text"             # text | json | status_code
    schema: dict[str, Any] = field(default_factory=dict)  # JSON Schema subset


@dataclass
class SkillPermissions:
    """Security boundaries for this skill."""
    network: bool = False            # allow network access?
    filesystem: str = "none"         # none | read_only | read_write
    allowed_commands: list[str] = field(default_factory=list)
    allowed_domains: list[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════════════════
# Skill data model
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Skill:
    """A named skill with explicit I/O contract, permissions, and lifecycle."""

    # Identity
    name: str
    version: str = "1.0.0"
    description: str = ""
    type: str = "skill"              # skill | tool | mcp | middleware
    tags: list[str] = field(default_factory=list)

    # Prompt (main body of SKILL.md)
    system_prompt: str = ""

    # I/O contract
    call: SkillCallSpec | None = None
    output: SkillOutputSpec | None = None

    # Triggers (for auto-detection)
    triggers: list[str] = field(default_factory=list)

    # Tool restrictions (empty = all tools allowed)
    tools: list[str] = field(default_factory=list)

    # Permissions
    permissions: SkillPermissions | None = None

    # Dependencies
    dependencies: list[str] = field(default_factory=list)

    # Model override
    model: str | None = None
    temperature: float | None = None

    # Extension metadata
    metadata: dict[str, Any] = field(default_factory=dict)

    # Runtime
    skill_dir: str = ""              # path to skill folder
    _handler: Callable | None = None  # loaded handler function

    # ── Serialization ────────────────────────────────────────────────────

    def to_frontmatter(self) -> str:
        """Export as SKILL.md format with full manifest."""
        d: dict[str, Any] = {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "type": self.type,
            "tags": self.tags,
        }
        if self.triggers:
            d["triggers"] = self.triggers
        if self.tools:
            d["tools"] = self.tools
        if self.model:
            d["model"] = self.model
        if self.dependencies:
            d["dependencies"] = self.dependencies
        if self.call:
            d["call"] = {
                "function": self.call.function,
                "parameters": self.call.parameters,
            }
        if self.output:
            d["output"] = {
                "format": self.output.format,
            }
            if self.output.schema:
                d["output"]["schema"] = self.output.schema
        if self.permissions:
            d["permissions"] = {
                "network": self.permissions.network,
                "filesystem": self.permissions.filesystem,
            }
            if self.permissions.allowed_commands:
                d["permissions"]["allowed_commands"] = self.permissions.allowed_commands
            if self.permissions.allowed_domains:
                d["permissions"]["allowed_domains"] = self.permissions.allowed_domains
        if self.metadata:
            d["metadata"] = self.metadata

        fm = (
            yaml.dump(d, default_flow_style=False, allow_unicode=True, sort_keys=False)
            if HAS_YAML
            else json.dumps(d, indent=2, ensure_ascii=False)
        )
        body = self.system_prompt or f"# {self.name}\n\n{self.description}"
        return f"---\n{fm}---\n\n{body}"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Skill":
        """Build a Skill from a flat dict (legacy JSON/YAML format)."""
        call_raw = d.get("call", {}) or {}
        output_raw = d.get("output", {}) or {}
        perm_raw = d.get("permissions", {}) or {}

        return cls(
            name=d.get("name", ""),
            version=d.get("version", "1.0.0"),
            description=d.get("description", ""),
            type=d.get("type", "skill"),
            tags=d.get("tags", []),
            system_prompt=d.get("system_prompt", ""),
            call=SkillCallSpec(
                function=call_raw.get("function", ""),
                parameters=call_raw.get("parameters", {}),
            ) if call_raw else None,
            output=SkillOutputSpec(
                format=output_raw.get("format", "text"),
                schema=output_raw.get("schema", {}),
            ) if output_raw else None,
            triggers=d.get("triggers", []),
            tools=d.get("tools", []),
            permissions=SkillPermissions(
                network=perm_raw.get("network", False),
                filesystem=perm_raw.get("filesystem", "none"),
                allowed_commands=perm_raw.get("allowed_commands", []),
                allowed_domains=perm_raw.get("allowed_domains", []),
            ) if perm_raw else None,
            dependencies=d.get("dependencies", []),
            model=d.get("model"),
            temperature=d.get("temperature"),
            metadata=d.get("metadata", {}),
        )

    @classmethod
    def from_frontmatter(cls, raw: str, source: str = "unknown",
                         skill_dir: str = "") -> "Skill | None":
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

        call_raw = meta.get("call", {}) or {}
        output_raw = meta.get("output", {}) or {}
        perm_raw = meta.get("permissions", {}) or {}

        return cls(
            name=meta.get("name", Path(source).stem),
            version=str(meta.get("version", "1.0.0")),
            description=meta.get("description", ""),
            type=meta.get("type", "skill"),
            tags=meta.get("tags", []),
            system_prompt=match.group(2).strip(),
            call=SkillCallSpec(
                function=call_raw.get("function", ""),
                parameters=call_raw.get("parameters", {}),
            ) if call_raw else None,
            output=SkillOutputSpec(
                format=output_raw.get("format", "text"),
                schema=output_raw.get("schema", {}),
            ) if output_raw else None,
            triggers=meta.get("triggers", []),
            tools=meta.get("tools", []),
            permissions=SkillPermissions(
                network=perm_raw.get("network", False),
                filesystem=perm_raw.get("filesystem", "none"),
                allowed_commands=perm_raw.get("allowed_commands", []),
                allowed_domains=perm_raw.get("allowed_domains", []),
            ) if (perm_raw.get("network") is not None
                  or perm_raw.get("filesystem")
                  or perm_raw.get("allowed_commands")
                  or perm_raw.get("allowed_domains")) else None,
            dependencies=meta.get("dependencies", []),
            model=meta.get("model"),
            temperature=meta.get("temperature"),
            metadata=meta.get("metadata", {}),
            skill_dir=skill_dir,
        )

    # ── Runtime: handler ────────────────────────────────────────────────

    def load_handler(self) -> bool:
        """Load handler.py from the skill directory. Returns True if found.

        The skill directory is temporarily added to ``sys.path`` so that
        handler.py can use absolute imports to reference sibling modules
        (e.g. ``from weather_utils import ...``).  It is removed after
        loading to prevent polluting the global import namespace.
        """
        if not self.skill_dir:
            return False
        handler_path = Path(self.skill_dir) / "handler.py"
        if not handler_path.exists():
            return False
        try:
            skill_dir = str(Path(self.skill_dir).resolve())
            # Temporarily add skill dir to sys.path for local imports
            path_added = skill_dir not in sys.path
            if path_added:
                sys.path.insert(0, skill_dir)

            spec = importlib.util.spec_from_file_location(
                f"ata_skill_{self.name}", str(handler_path)
            )
            if spec is None or spec.loader is None:
                return False
            module = importlib.util.module_from_spec(spec)
            # Set __package__ to allow relative imports within the skill
            module.__package__ = f"ata_skill_{self.name}"
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            self._handler = getattr(module, "run", None) or getattr(module, "handle", None)
            if self._handler:
                logger.debug("Loaded handler for skill %s", self.name)
                return True
        except Exception:
            logger.exception("Failed to load handler for skill %s", self.name)
        finally:
            # Restore sys.path
            if path_added:
                try:
                    sys.path.remove(skill_dir)
                except ValueError:
                    pass
        return False

    def run_handler(self, input_data: dict[str, Any]) -> Any:
        """Execute the skill's handler with structured input."""
        if not self._handler:
            self.load_handler()
        if not self._handler:
            raise RuntimeError(f"Skill {self.name} has no handler")
        try:
            return self._handler(input_data)
        except Exception:
            logger.exception("Handler failed for skill %s", self.name)
            raise

    @property
    def has_handler(self) -> bool:
        if self._handler:
            return True
        return bool(self.skill_dir and (Path(self.skill_dir) / "handler.py").exists())

    # ── Runtime: prompts ─────────────────────────────────────────────────

    def load_prompts(self) -> dict[str, str]:
        """
        Load all prompt templates from the prompts/ directory.
        Returns {name: content} dict. Supports .md, .txt, .prompt files.
        Cached after first load.
        """
        if not self.skill_dir:
            return {}
        prompts_dir = Path(self.skill_dir) / "prompts"
        if not prompts_dir.is_dir():
            return {}
        result: dict[str, str] = {}
        for fp in sorted(prompts_dir.glob("*")):
            if fp.suffix in (".md", ".txt", ".prompt"):
                try:
                    result[fp.stem] = fp.read_text(encoding="utf-8")
                except Exception:
                    logger.warning("Failed to read prompt %s", fp)
        return result

    def get_prompt_template(self, name: str) -> str | None:
        """
        Get a specific prompt template by name (without extension).
        Example: skill.get_prompt_template("system") → content of prompts/system.md
        """
        if not self.skill_dir:
            return None
        for ext in (".md", ".txt", ".prompt"):
            fp = Path(self.skill_dir) / "prompts" / f"{name}{ext}"
            if fp.exists():
                try:
                    return fp.read_text(encoding="utf-8")
                except Exception:
                    return None
        return None

    @property
    def prompt_names(self) -> list[str]:
        """List available prompt template names."""
        if not self.skill_dir:
            return []
        prompts_dir = Path(self.skill_dir) / "prompts"
        if not prompts_dir.is_dir():
            return []
        return sorted(
            fp.stem for fp in prompts_dir.glob("*")
            if fp.suffix in (".md", ".txt", ".prompt")
        )

    # ── Runtime: resources ───────────────────────────────────────────────

    def load_resources(self) -> dict[str, Any]:
        """
        Load all resource files from the resources/ directory.
        JSON files → parsed objects; .yaml/.yml → parsed; others → raw text.
        Cached after first load.
        """
        if not self.skill_dir:
            return {}
        res_dir = Path(self.skill_dir) / "resources"
        if not res_dir.is_dir():
            return {}
        result: dict[str, Any] = {}
        for fp in sorted(res_dir.glob("*")):
            if fp.name.startswith("."):
                continue
            try:
                if fp.suffix in (".json",):
                    result[fp.stem] = json.loads(fp.read_text(encoding="utf-8"))
                elif fp.suffix in (".yaml", ".yml") and HAS_YAML:
                    result[fp.stem] = yaml.safe_load(fp.read_text(encoding="utf-8"))
                elif fp.suffix in (".txt", ".csv", ".tsv"):
                    result[fp.stem] = fp.read_text(encoding="utf-8")
                elif fp.suffix in (".py",):
                    continue  # skip Python files in resources
                else:
                    result[fp.stem] = fp.read_text(encoding="utf-8")
            except Exception:
                logger.warning("Failed to load resource %s", fp)
        return result

    def get_resource(self, name: str) -> Any:
        """
        Get a specific resource by name (without extension).
        Example: skill.get_resource("config") → parsed JSON from resources/config.json
        """
        if not self.skill_dir:
            return None
        res_dir = Path(self.skill_dir) / "resources"
        if not res_dir.is_dir():
            return None
        for fp in sorted(res_dir.glob(f"{name}.*")):
            if fp.name.startswith("."):
                continue
            try:
                if fp.suffix in (".json",):
                    return json.loads(fp.read_text(encoding="utf-8"))
                if fp.suffix in (".yaml", ".yml") and HAS_YAML:
                    return yaml.safe_load(fp.read_text(encoding="utf-8"))
                return fp.read_text(encoding="utf-8")
            except Exception:
                logger.warning("Failed to read resource %s", fp)
        return None

    @property
    def resource_names(self) -> list[str]:
        """List available resource names."""
        if not self.skill_dir:
            return []
        res_dir = Path(self.skill_dir) / "resources"
        if not res_dir.is_dir():
            return []
        return sorted({fp.stem for fp in res_dir.glob("*") if not fp.name.startswith(".")})

    # ── Runtime: README ──────────────────────────────────────────────────

    def get_readme(self) -> str | None:
        """Read the skill's README.md if it exists."""
        if not self.skill_dir:
            return None
        fp = Path(self.skill_dir) / "README.md"
        if fp.exists():
            try:
                return fp.read_text(encoding="utf-8")
            except Exception:
                return None
        return None

    # ── Runtime: dependencies ────────────────────────────────────────────

    def get_dependencies(self) -> list[str]:
        """Parse requirements.txt from the skill directory. Returns list of package specs."""
        if not self.skill_dir:
            return []
        fp = Path(self.skill_dir) / "requirements.txt"
        if not fp.exists():
            return []
        try:
            lines = fp.read_text(encoding="utf-8").strip().splitlines()
            return [
                line.strip() for line in lines
                if line.strip() and not line.strip().startswith("#")
            ]
        except Exception:
            return []

    # ── Runtime: generic file access ────────────────────────────────────

    def read_file(self, relative_path: str) -> str | None:
        """
        Read ANY file within the skill folder.

        Example: skill.read_file(".env.example") → content
                 skill.read_file("prompts/system_prompt.txt") → content
        """
        if not self.skill_dir:
            return None
        fp = Path(self.skill_dir) / relative_path
        # Safety: prevent path traversal
        try:
            fp.resolve().relative_to(Path(self.skill_dir).resolve())
        except ValueError:
            logger.warning("Path traversal blocked: %s", relative_path)
            return None
        if not fp.is_file():
            return None
        try:
            return fp.read_text(encoding="utf-8")
        except Exception:
            logger.warning("Failed to read %s", fp)
            return None

    def read_json(self, relative_path: str) -> Any:
        """Read and parse a JSON file in the skill folder."""
        raw = self.read_file(relative_path)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON: %s", relative_path)
            return None

    def list_files(self, pattern: str = "*") -> list[str]:
        """
        List files in the skill folder matching a glob pattern.

        Example: skill.list_files("prompts/*.txt") → ["prompts/system_prompt.txt"]
        """
        if not self.skill_dir:
            return []
        base = Path(self.skill_dir)
        matches = sorted(base.glob(pattern))
        return [
            str(m.relative_to(base)).replace("\\", "/")
            for m in matches if m.is_file()
        ]

    def file_tree(self) -> str:
        """Return a plain-text tree of the skill folder (dir/, indent, file)."""
        if not self.skill_dir:
            return "(no directory)"
        base = Path(self.skill_dir)
        lines = [base.name + "/"]
        for fp in sorted(base.rglob("*")):
            if any(p.startswith(".") for p in fp.parts):
                continue
            if fp.name == "__pycache__":
                continue
            depth = len(fp.relative_to(base).parts)
            indent = "    " * (depth - 1)
            if fp.is_dir():
                lines.append(f"{indent}    {fp.name}/")
            else:
                lines.append(f"{indent}    {fp.name}")
        return "\n".join(lines)

    # ── Alternative manifest formats ─────────────────────────────────────

    @classmethod
    def from_manifest_json(cls, path: str | Path) -> "Skill | None":
        """Load skill from a manifest.json file."""
        fp = Path(path)
        if not fp.exists():
            return None
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Failed to parse %s: %s", fp, e)
            return None
        call_raw = data.get("call", {}) or {}
        output_raw = data.get("output", {}) or {}
        perm_raw = data.get("permissions", {}) or {}
        return cls(
            name=data.get("name", fp.parent.name),
            version=data.get("version", "1.0.0"),
            description=data.get("description", ""),
            type=data.get("type", "skill"),
            tags=data.get("tags", []),
            system_prompt=data.get("system_prompt", data.get("description", "")),
            call=SkillCallSpec(
                function=call_raw.get("function", ""),
                parameters=call_raw.get("parameters", {}),
            ) if call_raw else None,
            output=SkillOutputSpec(
                format=output_raw.get("format", "text"),
                schema=output_raw.get("schema", {}),
            ) if output_raw else None,
            triggers=data.get("triggers", []),
            tools=data.get("tools", []),
            permissions=SkillPermissions(
                network=perm_raw.get("network", False),
                filesystem=perm_raw.get("filesystem", "none"),
                allowed_commands=perm_raw.get("allowed_commands", []),
                allowed_domains=perm_raw.get("allowed_domains", []),
            ) if perm_raw else None,
            dependencies=data.get("dependencies", []),
            model=data.get("model"),
            metadata={
                **data.get("metadata", {}),
                **{k: data[k] for k in ("author", "license", "homepage")
                   if k in data and k not in data.get("metadata", {})},
            },
            skill_dir=str(fp.parent),
        )

    @classmethod
    def from_skill_yaml(cls, path: str | Path) -> "Skill | None":
        """Load skill from a skill.yaml or skill.yml file."""
        if not HAS_YAML:
            return None
        fp = Path(path)
        if not fp.exists():
            return None
        try:
            data = yaml.safe_load(fp.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Failed to parse %s: %s", fp, e)
            return None
        if not isinstance(data, dict):
            return None
        return cls.from_dict({**data, "skill_dir": str(fp.parent)})

    @property
    def safe_name(self) -> str:
        """Name safe for use as identifier."""
        return re.sub(r"[^a-zA-Z0-9_]", "_", self.name)

    def get_prompt(self) -> str:
        """Return system prompt (alias for system_prompt for Extension compat)."""
        return self.resolve_includes(self.system_prompt)

    def get_tools(self) -> list[str]:
        """Return tool restriction list (alias for tools for Extension compat)."""
        return self.tools

    def resolve_includes(self, text: str, _depth: int = 0) -> str:
        """
        Resolve @include directives in *text*.

        Syntax:
            @include path/to/file.md        — inline file content (relative to skill dir)
            @include prompts/system.txt     — load a prompt template
            @include resources/config.json  — load and inline as text

        The included file's content replaces the @include line.  Recursive
        includes are supported (max depth 5 to prevent infinite loops).

        Lines without a matching file are left as-is (no error — the LLM
        will see the raw directive and can ask for clarification).
        """
        if _depth > 5:
            return text

        resolved_lines: list[str] = []
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("@include "):
                rel_path = stripped[len("@include "):].strip()
                included = self.read_file(rel_path)
                if included is not None:
                    # Recursively resolve includes in the included file
                    included = self.resolve_includes(included, _depth + 1)
                    resolved_lines.append(included)
                else:
                    # File not found — leave directive as-is so the LLM can react
                    resolved_lines.append(line)
            else:
                resolved_lines.append(line)
        return "\n".join(resolved_lines)

    def __repr__(self) -> str:
        return f"Skill(name={self.name!r}, v{self.version}, type={self.type})"


# ═══════════════════════════════════════════════════════════════════════════════
# Skill manager
# ═══════════════════════════════════════════════════════════════════════════════

class SkillManager:
    """Loads skills from folder-based skill directories + flat legacy files."""

    def __init__(self, skills_dir: str | Path | None = None):
        if skills_dir is None:
            # Always use ~/.ata_coder/skills — seed if empty
            from .settings import init_settings
            try:
                settings = init_settings()
                skills_dir = settings.skills_dir
            except Exception:
                skills_dir = Path.home() / ".ata_coder" / "skills"
        self.skills_dir = Path(skills_dir)
        self.skills_dir.mkdir(parents=True, exist_ok=True)

        self._skills: dict[str, Skill] = {}
        self._active_skills: dict[str, Skill] = {}

        self._load_from_directory()

        # Log what we found
        logger.info("Skills loaded: %d from %s", len(self._skills), self.skills_dir)

    # ── Loading ─────────────────────────────────────────────────────────────

    def _load_from_directory(self) -> None:
        """Scan skills/ for:
        1. Subdirectories containing SKILL.md (primary format)
        2. Flat .md files (legacy — each is one skill)
        3. .json / .yaml files (legacy)
        """
        if not self.skills_dir.exists():
            return

        # ── Folder-based skills (primary) ──────────────────────────────
        for d in sorted(self.skills_dir.iterdir()):
            if not d.is_dir() or d.name.startswith(".") or d.name.startswith("_"):
                continue
            skill = None

            # Priority: SKILL.md → manifest.json → skill.yaml → *.md
            skill_md = d / "SKILL.md"
            manifest_json = d / "manifest.json"
            skill_yaml = d / "skill.yaml"
            skill_yml = d / "skill.yml"

            if skill_md.exists():
                try:
                    raw = skill_md.read_text(encoding="utf-8")
                    skill = Skill.from_frontmatter(raw, source=str(skill_md),
                                                   skill_dir=str(d))
                except Exception as e:
                    logger.warning("Failed to load SKILL.md from %s: %s", d, e)
            elif manifest_json.exists():
                skill = Skill.from_manifest_json(str(manifest_json))
            elif skill_yaml.exists():
                skill = Skill.from_skill_yaml(str(skill_yaml))
            elif skill_yml.exists():
                skill = Skill.from_skill_yaml(str(skill_yml))
            else:
                # Legacy fallback: first .md file in folder
                md_files = sorted(d.glob("*.md"))
                if md_files:
                    try:
                        raw = md_files[0].read_text(encoding="utf-8")
                        skill = Skill.from_frontmatter(raw, source=str(md_files[0]),
                                                       skill_dir=str(d))
                    except Exception as e:
                        logger.warning("Failed to load %s: %s", md_files[0], e)

            if skill and skill.name:
                self._skills[skill.name] = skill
                logger.debug("Loaded skill: %s from %s/", skill.name, d.name)

        # ── Flat .md files (legacy backward compat) ────────────────────
        for fp in sorted(self.skills_dir.glob("*.md")):
            try:
                raw = fp.read_text(encoding="utf-8")
                skill = Skill.from_frontmatter(raw, source=fp.name)
                if skill and skill.name not in self._skills:
                    self._skills[skill.name] = skill
                    logger.debug("Loaded legacy skill: %s from %s", skill.name, fp.name)
            except Exception as e:
                logger.warning("Failed to load legacy skill %s: %s", fp.name, e)

        # ── Legacy JSON/YAML ──────────────────────────────────────────
        for fp in self.skills_dir.glob("*.json"):
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                for item in (data if isinstance(data, list) else [data]):
                    skill = Skill.from_dict(item)
                    if skill.name and skill.name not in self._skills:
                        self._skills[skill.name] = skill
            except Exception as e:
                logger.warning("Failed to load %s: %s", fp.name, e)

        if HAS_YAML:
            for fp in list(self.skills_dir.glob("*.yaml")) + list(self.skills_dir.glob("*.yml")):
                try:
                    data = yaml.safe_load(fp.read_text(encoding="utf-8"))
                    for item in (data if isinstance(data, list) else [data]):
                        skill = Skill.from_dict(item)
                        if skill.name and skill.name not in self._skills:
                            self._skills[skill.name] = skill
                except Exception as e:
                    logger.warning("Failed to load %s: %s", fp.name, e)

        logger.debug("Loaded %d skills total", len(self._skills))

    # ── Management ──────────────────────────────────────────────────────────

    def list_skills(self) -> list[Skill]:
        return list(self._skills.values())

    def get_skill(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def activate(self, name: str, merge: bool = True) -> Skill | None:
        """Activate a skill. merge=True → multi-skill, merge=False → solo."""
        skill = self._skills.get(name)
        if skill:
            if not merge:
                self._active_skills.clear()
            self._active_skills[name] = skill
            logger.info("Activated: %s (active: %d)", name, len(self._active_skills))
        else:
            logger.warning("Skill not found: %s", name)
        return skill

    def deactivate(self, name: str | None = None) -> None:
        """Deactivate specific skill or all."""
        if name:
            self._active_skills.pop(name, None)
        else:
            self._active_skills.clear()

    @property
    def active_skill(self) -> Skill | None:
        """Backward-compat: first active skill."""
        for s in self._active_skills.values():
            return s
        return None

    @property
    def active_skills(self) -> list[Skill]:
        return list(self._active_skills.values())

    def get_system_prompt(self) -> str:
        """Aggregate prompts from all active skills."""
        if self._active_skills:
            sorted_skills = sorted(self._active_skills.values(), key=lambda s: s.name)
            parts = [s.system_prompt for s in sorted_skills if s.system_prompt]
            if parts:
                return "\n\n".join(parts)
        default = self._skills.get("general-coder")
        return default.system_prompt if default else (
            "You are an expert software engineer embedded in a coding agent. "
            "Understand intent, navigate the codebase, make precise surgical edits, "
            "verify changes, and communicate clearly. "
            "ONE problem per change. Read before edit. Match existing code style."
        )

    def get_allowed_tools(self) -> list[str] | None:
        """Intersection of tool restrictions from all active skills."""
        restrictions: list[set[str]] = []
        for skill in self._active_skills.values():
            if skill.tools:
                restrictions.append(set(skill.tools))
        if not restrictions:
            return None
        allowed = restrictions[0]
        for r in restrictions[1:]:
            allowed &= r
        return list(allowed) if allowed else None

    # ── Detection ───────────────────────────────────────────────────────────

    def _trigger_matches(self, trigger: str, text: str) -> bool:
        t = trigger.lower()
        words = t.split()
        return all(w in text for w in words) if len(words) > 1 else t in text

    def detect_skill(self, user_input: str) -> Skill | None:
        candidates = self.detect_skills(user_input)
        return candidates[0] if candidates else None

    def detect_skills(self, user_input: str, max_results: int = 3) -> list[Skill]:
        """Auto-detect matching skills from trigger keywords."""
        user_lower = user_input.lower()
        candidates: list[tuple[int, Skill]] = []
        for skill in self._skills.values():
            if not skill.triggers:
                continue
            score = sum(
                len(t.split())
                for t in skill.triggers
                if self._trigger_matches(t, user_lower)
            )
            if score > 0:
                candidates.append((score, skill))
        if not candidates:
            return []
        candidates.sort(key=lambda x: (-x[0], 1 if x[1].name == "general-coder" else 0))
        result = [skill for _, skill in candidates[:max_results]]
        if result and result[0].name == "general-coder" and len(result) > 1:
            result = result[1:] + result[:1]
        return result

    async def detect_skills_smart(self, user_input: str, max_results: int = 3,
                            llm_client=None) -> list[tuple[Skill, float]]:
        """Smart skill detection with LLM-based classification.

        Uses keyword matching as first pass, then LLM classification for
        ambiguous cases (multiple skills with similar scores, or low confidence).

        Args:
            user_input: The user's task/query
            max_results: Maximum number of skills to return
            llm_client: Optional LLM client for smart classification

        Returns:
            List of (Skill, confidence) tuples sorted by confidence descending
        """
        # Phase 1: Keyword-based scoring (fast, no API call)
        keyword_results = self.detect_skills(user_input, max_results=5)

        if not keyword_results:
            # No keyword match at all — try LLM if available, else default
            if llm_client:
                skill_name = await self._llm_classify(user_input, llm_client)
                if skill_name and skill_name in self._skills:
                    skill = self._skills[skill_name]
                    return [(skill, 0.7)]
            default = self._skills.get("general-coder")
            return [(default, 0.3)] if default else []

        # Phase 2: If we have 1 clear winner (score gap > 2x), use it
        if len(keyword_results) == 1:
            return [(keyword_results[0], 0.85)]

        # Calculate score-based confidences
        scores = {}
        for skill in keyword_results:
            triggers = getattr(skill, 'triggers', []) or []
            scores[skill.name] = sum(
                len(t.split()) for t in triggers
                if self._trigger_matches(t, user_input.lower())
            )

        top_score = max(scores.values()) if scores else 1
        ranked = []
        for skill in keyword_results:
            conf = min(0.9, scores.get(skill.name, 1) / max(top_score, 1))
            ranked.append((skill, round(conf, 2)))

        # Phase 3: LLM refinement for ambiguous cases
        # If top 2 skills are within 30% confidence of each other, use LLM
        if len(ranked) >= 2 and ranked[0][1] - ranked[1][1] < 0.3:
            if llm_client:
                skill_name = await self._llm_classify(user_input, llm_client)
                if skill_name and skill_name in self._skills:
                    skill = self._skills[skill_name]
                    # Insert LLM-chosen skill at top
                    ranked.insert(0, (skill, 0.8))
                    # Deduplicate
                    seen = set()
                    deduped = []
                    for s, c in ranked:
                        if s.name not in seen:
                            seen.add(s.name)
                            deduped.append((s, c))
                    ranked = deduped

        ranked.sort(key=lambda x: -x[1])
        return ranked[:max_results]

    async def _llm_classify(self, user_input: str, llm_client) -> str | None:
        """Use a cheap LLM call to classify which skill best fits the task."""
        skill_list = "\n".join(
            f"- {s.name}: {s.description[:100]}"
            for s in self._skills.values()
            if s.name != "general-coder"
        )
        prompt = (
            "You are a task router. Given a user's request, pick the SINGLE "
            "best-matching skill from the list below. Reply with ONLY the skill "
            "name, nothing else.\n\n"
            f"Skills:\n{skill_list}\n\n"
            f"User request: {user_input[:500]}\n\n"
            "Best skill name:"
        )
        try:
            msgs = [
                {"role": "system", "content": "You are a skill router. Reply with only the skill name."},
                {"role": "user", "content": prompt},
            ]
            # Use non-streaming call with minimal tokens
            response = await llm_client.chat(msgs, system_prompt="Reply with only one skill name.")
            name = response.get("content", "").strip().lower().split("\n")[0].strip().strip('"').strip("'")
            # Validate against known skills
            for sname in self._skills:
                if sname.lower() in name or name in sname.lower():
                    return sname
            return None
        except Exception:
            return None

    # ── Execution ───────────────────────────────────────────────────────────

    def execute_skill(self, name: str, input_data: dict[str, Any]) -> dict[str, Any]:
        """
        Execute a skill's handler with structured input.
        Returns {success, output, error, status_code}.
        """
        skill = self._skills.get(name)
        if not skill:
            return {"success": False, "output": None, "error": f"Skill not found: {name}", "status_code": 404}
        if not skill.has_handler:
            return {"success": False, "output": None, "error": f"Skill {name} has no handler", "status_code": 501}
        try:
            result = skill.run_handler(input_data)
            return {"success": True, "output": result, "error": None, "status_code": 200}
        except Exception as e:
            return {
                "success": False,
                "output": None,
                "error": f"{type(e).__name__}: {e}",
                "status_code": 500,
                "traceback": traceback.format_exc(),
            }

    # ── Persistence ─────────────────────────────────────────────────────────

    def save_skill(self, skill: Skill) -> Path:
        """Save a skill as a folder with SKILL.md."""
        skill_dir = self.skills_dir / skill.name
        skill_dir.mkdir(parents=True, exist_ok=True)
        fp = skill_dir / "SKILL.md"
        fp.write_text(skill.to_frontmatter(), encoding="utf-8")
        self._skills[skill.name] = skill
        return skill_dir

    def delete_skill(self, name: str) -> bool:
        """Delete a skill folder and all contents."""
        import shutil
        skill_dir = self.skills_dir / name
        if skill_dir.exists() and skill_dir.is_dir():
            shutil.rmtree(skill_dir, ignore_errors=True)
            self._skills.pop(name, None)
            logger.info("Deleted skill folder: %s", name)
            return True
        # Legacy flat files
        for ext in (".md", ".json", ".yaml", ".yml"):
            fp = self.skills_dir / f"{name}{ext}"
            if fp.exists():
                fp.unlink()
                self._skills.pop(name, None)
                return True
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# Global singleton
# ═══════════════════════════════════════════════════════════════════════════════

_skill_manager: SkillManager | None = None


def get_skill_manager(skills_dir: str | None = None) -> SkillManager:
    """Get the global SkillManager singleton."""
    global _skill_manager
    if _skill_manager is None:
        _skill_manager = SkillManager(skills_dir)
    return _skill_manager
