"""Tests for the YAML-based workflow DSL."""

import tempfile
from pathlib import Path

import pytest

from ata_coder.workflow_dsl import (
    WorkflowDef,
    WorkflowRunner,
    StepResult,
    parse_workflow,
    parse_workflow_file,
)


class TestParseWorkflow:
    """YAML parsing and validation."""

    def test_parse_minimal_workflow(self):
        """Parse the simplest valid workflow."""
        yaml_text = """
name: minimal
steps:
  - id: step1
    run: echo hello
"""
        wf = parse_workflow(yaml_text)
        assert wf.name == "minimal"
        assert len(wf.steps) == 1
        assert wf.steps[0]["id"] == "step1"
        assert wf.steps[0]["run"] == "echo hello"

    def test_parse_auto_generates_step_ids(self):
        """Steps without explicit IDs get auto-generated ones."""
        yaml_text = """
name: auto-ids
steps:
  - run: cmd1
  - run: cmd2
"""
        wf = parse_workflow(yaml_text)
        assert wf.steps[0]["id"] == "step-1"
        assert wf.steps[1]["id"] == "step-2"

    def test_parse_missing_name_defaults(self):
        """Workflow without a name defaults to 'unnamed'."""
        wf = parse_workflow("steps:\n  - run: echo hi")
        assert wf.name == "unnamed"

    def test_parse_missing_steps_raises(self):
        """Workflow without steps should raise ValueError."""
        with pytest.raises(ValueError, match="step"):
            parse_workflow("name: no-steps")

    def test_parse_empty_steps_raises(self):
        """Workflow with empty steps list should raise ValueError."""
        yaml_text = """
name: empty
steps: []
"""
        with pytest.raises(ValueError, match="step"):
            parse_workflow(yaml_text)

    def test_parse_with_vars(self):
        """Vars section should be parsed."""
        yaml_text = """
name: with-vars
vars:
  target: production
  retries: 3
steps:
  - run: deploy
"""
        wf = parse_workflow(yaml_text)
        assert wf.vars["target"] == "production"
        assert wf.vars["retries"] == 3

    def test_parse_with_description(self):
        """Description should be parsed."""
        yaml_text = """
name: desc-test
description: A test workflow with a description
steps:
  - run: test
"""
        wf = parse_workflow(yaml_text)
        assert wf.description == "A test workflow with a description"

    def test_parse_parallel_step(self):
        """Parallel step with sub-steps (stored as dicts by YAML parser)."""
        yaml_text = """
name: parallel-test
steps:
  - id: fanout
    parallel:
      - run: task1
      - run: task2
      - agent: Review the code
"""
        wf = parse_workflow(yaml_text)
        assert len(wf.steps) == 1
        step = wf.steps[0]
        assert len(step["parallel"]) == 3
        # YAML parses "key: value" as dicts: {"run": "task1"}, {"agent": "Review..."}
        assert step["parallel"][0]["run"] == "task1"
        assert step["parallel"][2]["agent"] == "Review the code"

    def test_parse_depends_on(self):
        """Dependency specification."""
        yaml_text = """
name: deps
steps:
  - id: build
    run: make
  - id: test
    run: make test
    depends_on: [build]
"""
        wf = parse_workflow(yaml_text)
        assert wf.steps[1]["depends_on"] == ["build"]

    def test_parse_depends_on_string(self):
        """Single dependency as string (not list) should be normalized."""
        yaml_text = """
name: single-dep
steps:
  - id: a
    run: cmd-a
  - id: b
    run: cmd-b
    depends_on: a
"""
        wf = parse_workflow(yaml_text)
        # The parser accepts string form; runner normalizes it
        assert wf.steps[1]["depends_on"] == "a"

    def test_parse_workflow_file(self):
        """parse_workflow_file reads from a .yaml file."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as f:
            f.write("name: file-test\nsteps:\n  - run: cmd\n")
            f.flush()
            wf = parse_workflow_file(f.name)
            assert wf.name == "file-test"
        Path(f.name).unlink()

    def test_parse_file_not_found(self):
        """Missing file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            parse_workflow_file("/nonexistent/workflow.yaml")


class TestStepResult:
    """StepResult dataclass."""

    def test_success_result(self):
        r = StepResult(step_id="test", success=True, output="done")
        assert r.success
        assert r.output == "done"
        assert r.error == ""

    def test_failure_result(self):
        r = StepResult(step_id="test", success=False, error="boom")
        assert not r.success
        assert r.error == "boom"


class TestWorkflowRunner:
    """Runner execution logic (unit tests, no real agent)."""

    def test_runner_instantiation(self):
        """Runner can be created without a controller."""
        runner = WorkflowRunner()
        assert runner._controller is None

    def test_resolve_vars_simple(self):
        """Variable resolution replaces placeholders."""
        runner = WorkflowRunner()
        runner._vars["name"] = "Alice"
        result = runner._resolve_vars("Hello ${{ name }}!")
        assert result == "Hello Alice!"

    def test_resolve_vars_from_step_output(self):
        """Variable resolution reads from step results."""
        runner = WorkflowRunner()
        runner._results["build"] = StepResult(
            step_id="build", success=True, output="Build v1.0"
        )
        result = runner._resolve_vars("Deploying ${{ build }}")
        assert result == "Deploying Build v1.0"

    def test_resolve_vars_no_match(self):
        """Unknown placeholder is left unchanged."""
        runner = WorkflowRunner()
        result = runner._resolve_vars("Value: ${{ unknown_var }}")
        assert "${{ unknown_var }}" in result

    def test_resolve_vars_dict(self):
        """Variable resolution recurses into dicts."""
        runner = WorkflowRunner()
        runner._vars["env"] = "prod"
        result = runner._resolve_vars({"cmd": "deploy ${{ env }}"})
        assert result == {"cmd": "deploy prod"}

    def test_eval_condition_success_true(self):
        """eval_condition checks step result attributes."""
        runner = WorkflowRunner()
        runner._results["lint"] = StepResult(step_id="lint", success=True, output="ok")
        assert runner._eval_condition("lint.success == true")

    def test_eval_condition_success_false(self):
        """eval_condition returns False when step failed."""
        runner = WorkflowRunner()
        runner._results["lint"] = StepResult(step_id="lint", success=False, error="fail")
        assert runner._eval_condition("lint.success == false")

    def test_eval_condition_missing_step(self):
        """eval_condition returns False for missing step."""
        runner = WorkflowRunner()
        assert not runner._eval_condition("nonexistent.success == true")

    async def test_run_without_controller(self):
        """Running without a controller should fail gracefully."""
        runner = WorkflowRunner()
        wf = WorkflowDef(name="test", steps=[{"id": "s1", "run": "echo hi"}])
        results = await runner.run(wf)
        assert "s1" in results
        assert not results["s1"].success
        assert "No agent controller" in results["s1"].error
