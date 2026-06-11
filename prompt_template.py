"""
Prompt template engine with variable substitution and context injection.

Templates use a simple syntax:
- {{ variable }} — substituted from context
- {{% if condition %}}...{{% endif %}} — conditional blocks
- {{% for item in list %}}...{{% endfor %}} — loop blocks
- {{ project_structure }} — built-in function to inject project tree
- {{ git_status }} — built-in function to inject git status
- {{ recent_files }} — built-in function to inject recently modified files
- {{ memory_context }} — built-in function to inject relevant memories

Templates are loaded from files in prompts/ directory.
"""

import logging
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ── Template context ─────────────────────────────────────────────────────────

class TemplateContext:
    """
    Holds all variables and context functions available during template rendering.
    """

    def __init__(self, variables: dict[str, Any] | None = None):
        self.variables: dict[str, Any] = variables or {}
        self._fn_cache: dict[str, str] = {}

    def get(self, key: str, default: Any = "") -> Any:
        """Get a variable value."""
        # Check variables first
        if key in self.variables:
            return self.variables[key]

        # Check built-in functions
        fn = getattr(self, f"_fn_{key}", None)
        if fn:
            if key not in self._fn_cache:
                try:
                    self._fn_cache[key] = fn()
                except Exception as e:
                    logger.warning("Template function %s failed: %s", key, e)
                    self._fn_cache[key] = f"[Error: {e}]"
            return self._fn_cache[key]

        return default

    def set(self, key: str, value: Any) -> None:
        """Set a variable."""
        self.variables[key] = value

    # ── Built-in context functions ────────────────────────────────────────

    def _fn_now(self) -> str:
        """Current date/time."""
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _fn_date(self) -> str:
        """Current date."""
        return datetime.now().strftime("%Y-%m-%d")

    def _fn_workspace(self) -> str:
        """Workspace directory path."""
        return str(Path.cwd())

    def _fn_os(self) -> str:
        """Operating system info."""
        import platform
        return f"{platform.system()} {platform.release()}"

    def _fn_python_version(self) -> str:
        """Python version."""
        import platform
        return platform.python_version()

    def _fn_project_structure(self) -> str:
        """Generate a tree view of the project."""
        workspace = Path(self.variables.get("workspace_dir", Path.cwd()))
        lines = []
        try:
            for root, dirs, files in os.walk(workspace):
                # Skip hidden and common ignore dirs
                dirs[:] = [
                    d for d in sorted(dirs)
                    if not d.startswith(".")
                    and d not in (
                        "node_modules", "__pycache__", ".git",
                        "venv", ".venv", "dist", "build",
                        "target", ".next", "coverage",
                    )
                ]
                level = root.replace(str(workspace), "").count(os.sep)
                indent = "  " * level
                if level <= 3:  # limit depth
                    if level > 0:
                        lines.append(f"{indent}{os.path.basename(root)}/")
                    for f in sorted(files)[:30]:  # limit files per dir
                        lines.append(f"{indent}  {f}")
            return "\n".join(lines[:200])  # total limit
        except Exception as e:
            return f"[Error reading project structure: {e}]"

    def _fn_git_status(self) -> str:
        """Get git status summary."""
        workspace = self.variables.get("workspace_dir", Path.cwd())
        try:
            result = subprocess.run(
                ["git", "status", "--short"],
                capture_output=True, text=True,
                cwd=str(workspace), timeout=10,
            )
            if result.returncode == 0:
                output = result.stdout.strip()
                return output if output else "(clean working tree)"
            return "(not a git repository or git not available)"
        except Exception:
            return "(git not available)"

    def _fn_git_branch(self) -> str:
        """Get current git branch."""
        workspace = self.variables.get("workspace_dir", Path.cwd())
        try:
            result = subprocess.run(
                ["git", "branch", "--show-current"],
                capture_output=True, text=True,
                cwd=str(workspace), timeout=10,
            )
            return result.stdout.strip() or "(no branch)"
        except Exception:
            return "(git not available)"

    def _fn_recent_files(self) -> str:
        """List recently modified files."""
        workspace = Path(self.variables.get("workspace_dir", Path.cwd()))
        files = []
        try:
            for root, dirs, filenames in os.walk(workspace):
                dirs[:] = [
                    d for d in dirs
                    if not d.startswith(".")
                    and d not in ("node_modules", "__pycache__", ".git")
                ]
                for f in filenames:
                    fp = os.path.join(root, f)
                    try:
                        mtime = os.path.getmtime(fp)
                        files.append((mtime, fp))
                    except OSError:
                        pass
            files.sort(reverse=True)
            recent = files[:20]
            lines = []
            for mtime, fp in recent:
                rel = os.path.relpath(fp, str(workspace))
                dt = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
                lines.append(f"  {dt}  {rel}")
            return "\n".join(lines) if lines else "(no files found)"
        except Exception as e:
            return f"[Error: {e}]"


# ── Template parser / renderer ───────────────────────────────────────────────

class PromptTemplate:
    """
    A prompt template with variable substitution and conditionals.
    """

    def __init__(self, source: str, name: str = "inline"):
        self.name = name
        self.source = source

    def render(self, context: TemplateContext | None = None, **kwargs) -> str:
        """
        Render the template with the given context.

        Args:
            context: TemplateContext with variables
            **kwargs: Additional variables to add to context
        """
        if context is None:
            context = TemplateContext()
        for k, v in kwargs.items():
            context.set(k, v)

        result = self._render(self.source, context)
        return result

    def _render(self, source: str, context: TemplateContext) -> str:
        """Recursive template renderer."""
        # First, handle for loops
        source = self._expand_for(source, context)
        # Then, handle conditionals
        source = self._expand_if(source, context)
        # Finally, handle variable substitution
        source = self._expand_vars(source, context)
        return source

    def _expand_vars(self, source: str, context: TemplateContext) -> str:
        """Replace {{ variable }} placeholders."""
        def replacer(match):
            expr = match.group(1).strip()
            # Handle {{ var }} or {{ var | default }}
            if "|" in expr:
                var, default = expr.split("|", 1)
                value = context.get(var.strip(), default.strip())
            else:
                value = context.get(expr, "")
            return str(value) if value is not None else ""

        return re.sub(r"\{\{\s*(.+?)\s*\}\}", replacer, source)

    def _expand_if(self, source: str, context: TemplateContext) -> str:
        """Handle {{% if condition %}}...{{% endif %}} blocks."""
        def replacer(match):
            condition = match.group(1).strip()
            body = match.group(2)

            # Negation
            negate = condition.startswith("not ")
            if negate:
                condition = condition[4:]

            value = context.get(condition, "")
            is_truthy = bool(value)

            if negate:
                is_truthy = not is_truthy

            return body if is_truthy else ""

        return re.sub(
            r"\{\%\s*if\s+(.+?)\s*\%\}(.*?)\{\%\s*endif\s*\%\}",
            replacer,
            source,
            flags=re.DOTALL,
        )

    def _expand_for(self, source: str, context: TemplateContext) -> str:
        """Handle {{% for item in list %}}...{{% endfor %}} blocks."""
        def replacer(match):
            var_name = match.group(1).strip()
            list_name = match.group(2).strip()
            body = match.group(3)

            items = context.get(list_name, [])
            if isinstance(items, str):
                items = [items]
            if not isinstance(items, (list, tuple)):
                items = [str(items)]

            result = []
            for item in items:
                # Create a sub-context with the loop variable
                item_context = TemplateContext({**context.variables})
                item_context.set(var_name, item)
                result.append(body.replace(
                    f"{{{{ {var_name} }}}}", str(item)
                ))
            return "\n".join(result)

        return re.sub(
            r"\{\%\s*for\s+(\w+)\s+in\s+(\w+)\s*\%\}(.*?)\{\%\s*endfor\s*\%\}",
            replacer,
            source,
            flags=re.DOTALL,
        )


# ── Template manager ─────────────────────────────────────────────────────────

class TemplateManager:
    """
    Manages prompt templates: loading from files, rendering, and caching.
    """

    def __init__(self, prompts_dir: str | Path | None = None):
        if prompts_dir is None:
            prompts_dir = Path(__file__).parent / "prompts"
        self.prompts_dir = Path(prompts_dir)
        self._templates: dict[str, PromptTemplate] = {}
        self._load_templates()

    def _load_templates(self) -> None:
        """Load all template files from the prompts directory."""
        if not self.prompts_dir.exists():
            return

        for ext in ("*.md", "*.txt", "*.tmpl"):
            for file_path in self.prompts_dir.glob(ext):
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        source = f.read()
                    name = file_path.stem
                    self._templates[name] = PromptTemplate(source, name=name)
                    logger.debug("Loaded template: %s", name)
                except Exception as e:
                    logger.warning("Failed to load template %s: %s", file_path, e)

        logger.debug("Loaded %d templates", len(self._templates))

    def get(self, name: str) -> PromptTemplate | None:
        """Get a template by name."""
        return self._templates.get(name)

    def render(self, name: str, **kwargs) -> str | None:
        """Render a named template."""
        template = self._templates.get(name)
        if template is None:
            return None
        return template.render(**kwargs)

    def list_templates(self) -> list[str]:
        return list(self._templates.keys())

    def register(self, name: str, source: str) -> PromptTemplate:
        """Register an inline template."""
        template = PromptTemplate(source, name=name)
        self._templates[name] = template
        return template


# ── Build system prompt from template ────────────────────────────────────────

def build_system_prompt(
    skill_prompt: str,
    context: TemplateContext | None = None,
    template_manager: TemplateManager | None = None,
) -> str:
    """
    Build a complete system prompt by combining:
    1. The skill's system prompt template
    2. Injected context (workspace, git, project structure)
    3. Memory recall
    """
    if context is None:
        context = TemplateContext()

    template = PromptTemplate(skill_prompt)

    prompt = template.render(context)

    # Add environment context
    prompt += f"""

## Environment Context
- Workspace: {context.get('workspace', 'unknown')}
- Date: {context.get('date', 'unknown')}
- OS: {context.get('os', 'unknown')}
- Git branch: {context.get('git_branch', 'unknown')}
"""

    # Add project structure if available
    structure = context.get("project_structure", "")
    if structure:
        prompt += f"\n## Project Structure\n```\n{structure}\n```\n"

    # Add git status if there are changes
    git_status = context.get("git_status", "")
    if git_status and git_status != "(clean working tree)":
        prompt += f"\n## Git Status\n```\n{git_status}\n```\n"

    # Add memory context
    memory_ctx = context.get("memory_context", "")
    if memory_ctx:
        prompt += memory_ctx

    return prompt
