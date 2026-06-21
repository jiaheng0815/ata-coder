"""
Tests for prompt_template — template rendering, variable substitution,
conditionals, for loops, context injection.
"""

from ata_coder.prompt_template import (
    PromptTemplate,
    TemplateContext,
    TemplateManager,
    build_system_prompt,
)


class TestTemplateContext:
    """TemplateContext variable storage and built-in functions."""

    def test_set_and_get_variable(self):
        """set() and get() should store and retrieve variables."""
        ctx = TemplateContext()
        ctx.set("name", "world")
        assert ctx.get("name") == "world"

    def test_get_default(self):
        """get() should return default for missing keys."""
        ctx = TemplateContext()
        assert ctx.get("nonexistent", "fallback") == "fallback"
        assert ctx.get("nonexistent") == ""

    def test_initial_variables(self):
        """Variables passed at construction should be available."""
        ctx = TemplateContext({"name": "Alice", "count": 42})
        assert ctx.get("name") == "Alice"
        assert ctx.get("count") == 42

    def test_now_returns_string(self):
        """Built-in 'now' should return a date/time string."""
        ctx = TemplateContext()
        now = ctx.get("now")
        assert isinstance(now, str)
        assert len(now) > 0

    def test_date_returns_string(self):
        """Built-in 'date' should return a date string."""
        ctx = TemplateContext()
        date = ctx.get("date")
        assert isinstance(date, str)
        assert len(date) == 10  # YYYY-MM-DD

    def test_workspace_returns_string(self):
        """Built-in 'workspace' should return a path string."""
        ctx = TemplateContext()
        ws = ctx.get("workspace")
        assert isinstance(ws, str)

    def test_os_returns_string(self):
        """Built-in 'os' should return OS info."""
        ctx = TemplateContext()
        os_info = ctx.get("os")
        assert isinstance(os_info, str)

    def test_python_version_returns_string(self):
        """Built-in 'python_version' should return version string."""
        ctx = TemplateContext()
        ver = ctx.get("python_version")
        assert isinstance(ver, str)
        assert ver.startswith("3.")


class TestPromptTemplateVariableSubstitution:
    """PromptTemplate {{ variable }} substitution."""

    def test_simple_variable(self):
        """Simple {{ variable }} should be replaced."""
        tmpl = PromptTemplate("Hello, {{ name }}!")
        result = tmpl.render(TemplateContext({"name": "World"}))
        assert result == "Hello, World!"

    def test_multiple_variables(self):
        """Multiple variables should all be replaced."""
        tmpl = PromptTemplate("{{ greeting }}, {{ name }}!")
        result = tmpl.render(TemplateContext({"greeting": "Hi", "name": "Alice"}))
        assert result == "Hi, Alice!"

    def test_missing_variable_empty(self):
        """Missing variable should render as empty string."""
        tmpl = PromptTemplate("Hello, {{ name }}!")
        result = tmpl.render(TemplateContext())
        assert result == "Hello, !"

    def test_variable_with_default(self):
        """{{ var | default }} should use default when var is missing."""
        tmpl = PromptTemplate("Hello, {{ name | World }}!")
        result = tmpl.render(TemplateContext())
        assert result == "Hello, World!"

    def test_variable_with_default_present(self):
        """When variable is present, default should be ignored."""
        tmpl = PromptTemplate("Hello, {{ name | World }}!")
        result = tmpl.render(TemplateContext({"name": "Alice"}))
        assert result == "Hello, Alice!"

    def test_kwargs_override(self):
        """kwargs passed to render() should override context vars."""
        ctx = TemplateContext({"name": "Bob"})
        tmpl = PromptTemplate("Hello, {{ name }}!")
        result = tmpl.render(ctx, name="Alice")
        assert result == "Hello, Alice!"

    def test_builtin_function_in_template(self):
        """Built-in functions like {{ now }} should work."""
        tmpl = PromptTemplate("Today is {{ date }}.")
        result = tmpl.render(TemplateContext())
        assert "Today is" in result


class TestPromptTemplateConditionals:
    """{{% if condition %}}...{{% endif %}} blocks."""

    def test_if_true(self):
        """Block should render when condition is truthy."""
        tmpl = PromptTemplate("{{% if show %}}VISIBLE{{% endif %}}")
        result = tmpl.render(TemplateContext({"show": "yes"}))
        assert result == "VISIBLE"

    def test_if_false(self):
        """Block should be empty when condition is falsy."""
        tmpl = PromptTemplate("{{% if show %}}VISIBLE{{% endif %}}")
        result = tmpl.render(TemplateContext({"show": ""}))
        assert result == ""

    def test_if_not_true(self):
        """{{% if not condition %}} should negate."""
        tmpl = PromptTemplate("{{% if not hidden %}}VISIBLE{{% endif %}}")
        result = tmpl.render(TemplateContext({"hidden": ""}))
        assert result == "VISIBLE"

    def test_if_not_false(self):
        """{{% if not condition %}} with truthy value should hide."""
        tmpl = PromptTemplate("{{% if not hidden %}}VISIBLE{{% endif %}}")
        result = tmpl.render(TemplateContext({"hidden": "yes"}))
        assert result == ""


class TestPromptTemplateForLoops:
    """{{% for item in list %}}...{{% endfor %}} blocks."""

    def test_for_loop(self):
        """Simple for loop should iterate over list."""
        tmpl = PromptTemplate("{{% for item in items %}}{{ item }}\n{{% endfor %}}")
        result = tmpl.render(TemplateContext({"items": ["a", "b", "c"]}))
        assert "a" in result
        assert "b" in result
        assert "c" in result

    def test_for_loop_single_item(self):
        """For loop with single string should still work."""
        tmpl = PromptTemplate("{{% for item in items %}}{{ item }}{{% endfor %}}")
        result = tmpl.render(TemplateContext({"items": "hello"}))
        assert result == "hello"

    def test_for_loop_empty(self):
        """Empty list should produce empty output."""
        tmpl = PromptTemplate("{{% for item in items %}}{{ item }}{{% endfor %}}")
        result = tmpl.render(TemplateContext({"items": []}))
        assert result == ""


class TestComplexTemplate:
    """Complex templates combining all features."""

    def test_combined_features(self):
        """Variables + conditionals + loops should work together."""
        source = """Hello {{ name }}!
{{% if show_tools %}}
Tools:
{{% for tool in tools %}}
  - {{ tool }}
{{% endfor %}}
{{% endif %}}"""
        tmpl = PromptTemplate(source)
        ctx = TemplateContext({
            "name": "Alice",
            "show_tools": "yes",
            "tools": ["read_file", "write_file", "run_shell"],
        })
        result = tmpl.render(ctx)
        assert "Hello Alice" in result
        assert "read_file" in result
        assert "write_file" in result
        assert "run_shell" in result

    def test_conditional_hidden(self):
        """When conditional is false, that block should not appear."""
        source = """Base content.
{{% if extra %}}EXTRA CONTENT{{% endif %}}"""
        tmpl = PromptTemplate(source)
        result = tmpl.render(TemplateContext({"extra": ""}))
        assert "Base content" in result
        assert "EXTRA CONTENT" not in result


class TestTemplateManager:
    """TemplateManager file loading and caching."""

    def test_register_inline_template(self):
        """register() should add an inline template."""
        manager = TemplateManager(prompts_dir="/tmp/nonexistent_prompts")
        manager.register("greeting", "Hello, {{ name }}!")
        template = manager.get("greeting")
        assert template is not None
        result = template.render(name="World")
        assert result == "Hello, World!"

    def test_get_nonexistent(self):
        """get() should return None for missing templates."""
        manager = TemplateManager(prompts_dir="/tmp/nonexistent_prompts")
        assert manager.get("nonexistent") is None

    def test_render_nonexistent(self):
        """render() should return None for missing templates."""
        manager = TemplateManager(prompts_dir="/tmp/nonexistent_prompts")
        assert manager.render("nonexistent") is None

    def test_list_templates_empty(self):
        """list_templates() should return empty list for empty dir."""
        manager = TemplateManager(prompts_dir="/tmp/nonexistent_prompts")
        assert manager.list_templates() == []

    def test_list_templates_with_registered(self):
        """list_templates() should include registered templates."""
        manager = TemplateManager(prompts_dir="/tmp/nonexistent_prompts")
        manager.register("test", "content")
        assert "test" in manager.list_templates()


class TestBuildSystemPrompt:
    """build_system_prompt function."""

    def test_basic_prompt(self):
        """Basic system prompt should render and include context."""
        skill_prompt = "You are a {{ role }}."
        context = TemplateContext({
            "role": "coding assistant",
            "workspace": "/home/project",
            "date": "2025-01-01",
            "os": "Linux",
            "git_branch": "main",
        })
        result = build_system_prompt(skill_prompt, context)
        assert "You are a coding assistant." in result
        assert "## Environment Context" in result
        assert "Workspace:" in result

    def test_with_project_structure(self):
        """If project_structure is set, it should be included."""
        skill_prompt = "You are an expert."
        context = TemplateContext({
            "role": "expert",
            "workspace": "/home/project",
            "date": "2025-01-01",
            "os": "Linux",
            "git_branch": "main",
            "project_structure": "src/\n  main.py\n  utils.py\n",
        })
        result = build_system_prompt(skill_prompt, context)
        assert "## Project Structure" in result
        assert "main.py" in result

    def test_with_git_status(self):
        """If git_status is set and non-trivial, it should be included."""
        skill_prompt = "You are an expert."
        context = TemplateContext({
            "workspace": "/home/project",
            "date": "2025-01-01",
            "os": "Linux",
            "git_branch": "main",
            "git_status": "M src/main.py\n?? new_file.py",
        })
        result = build_system_prompt(skill_prompt, context)
        assert "## Git Status" in result
        assert "src/main.py" in result

    def test_with_clean_git_status(self):
        """Clean git status should not be included."""
        skill_prompt = "You are an expert."
        context = TemplateContext({
            "workspace": "/home/project",
            "date": "2025-01-01",
            "os": "Linux",
            "git_branch": "main",
            "git_status": "(clean working tree)",
        })
        result = build_system_prompt(skill_prompt, context)
        assert "## Git Status" not in result

    def test_with_memory_context(self):
        """If memory_context is set, it should be appended."""
        skill_prompt = "You are an expert."
        context = TemplateContext({
            "workspace": "/home/project",
            "date": "2025-01-01",
            "os": "Linux",
            "git_branch": "main",
            "memory_context": "\n## Relevant Memories\n- User prefers dark mode",
        })
        result = build_system_prompt(skill_prompt, context)
        assert "## Relevant Memories" in result
