"""
YAML-based Workflow DSL for ATA Coder.

Lets users define multi-step automation workflows that orchestrate
tools, agents, and conditional logic — all in a simple YAML format.

Features:
- Sequential and parallel step execution
- Conditional branching (if/then/else)
- Tool invocation (any registered tool)
- Agent sub-tasks with model selection
- File input/output piping between steps
- Dependency graph (depends_on)

Schema example::

    name: code-review-pipeline
    description: Full code review workflow
    steps:
      - id: lint
        tool: run_shell
        args: { command: "ruff check ." }

      - id: review
        agent: "Review the code changes for bugs and security issues"
        model: opus
        depends_on: [lint]

      - id: fix
        agent: "Apply the review fixes"
        model: sonnet
        depends_on: [review]
        condition: "review.found_issues == true"

Usage:
    from .workflow_dsl import WorkflowRunner

    runner = WorkflowRunner(agent_controller)
    result = await runner.run_file("workflows/deploy.yaml")
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .utils import try_import_yaml

logger = logging.getLogger(__name__)

_yaml_mod, HAS_YAML = try_import_yaml()

if not HAS_YAML:
    # Fallback: basic YAML support via json (limited but functional for simple cases)
    _yaml_mod = None


# ── Step types ────────────────────────────────────────────────────────────────

@dataclass
class StepResult:
    """Result of executing one workflow step."""
    step_id: str
    success: bool
    output: str = ""
    error: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowDef:
    """Parsed workflow definition."""
    name: str
    description: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    vars: dict[str, Any] = field(default_factory=dict)


# ── Parser ────────────────────────────────────────────────────────────────────

def parse_workflow(yaml_text: str) -> WorkflowDef:
    """Parse a YAML workflow definition.

    Args:
        yaml_text: Raw YAML string.

    Returns:
        Parsed WorkflowDef.

    Raises:
        ValueError: If the YAML is invalid or missing required fields.
    """
    if _yaml_mod is not None:
        try:
            data = _yaml_mod.safe_load(yaml_text)
        except Exception as e:
            raise ValueError(f"Invalid YAML: {e}") from e
    else:
        import json as _json
        try:
            data = _json.loads(yaml_text)
        except Exception:
            raise ValueError(
                "YAML support requires PyYAML. Install with: pip install pyyaml"
            )

    if not isinstance(data, dict):
        raise ValueError("Workflow YAML must be a mapping (top-level dict)")

    name = data.get("name", "unnamed")
    if not name:
        raise ValueError("Workflow requires a 'name' field")

    description = data.get("description", "")
    steps = data.get("steps", [])
    vars_ = data.get("vars", {})

    if not steps:
        raise ValueError("Workflow requires at least one 'step'")

    # Validate each step
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError(f"Step {i} must be a mapping")
        if "id" not in step:
            step["id"] = f"step-{i+1}"

    return WorkflowDef(name=name, description=description, steps=steps, vars=vars_)


def parse_workflow_file(path: str | Path) -> WorkflowDef:
    """Parse a workflow from a .yaml or .yml file."""
    filepath = Path(path)
    if not filepath.exists():
        raise FileNotFoundError(f"Workflow file not found: {path}")
    text = filepath.read_text(encoding="utf-8")
    return parse_workflow(text)


# ── Runner ────────────────────────────────────────────────────────────────────

class WorkflowRunner:
    """Executes a parsed WorkflowDef against an agent controller.

    Supports sequential (default) and parallel step execution with
    dependency ordering.
    """

    def __init__(self, agent_controller: Any = None):
        """Initialize with an optional agent controller.

        Args:
            agent_controller: AgentController instance for running agent tasks.
                              If None, only tool-based steps work.
        """
        self._controller = agent_controller
        self._results: dict[str, StepResult] = {}
        self._vars: dict[str, Any] = {}

    # ── Public API ────────────────────────────────────────────────────────

    async def run(self, workflow: WorkflowDef) -> dict[str, StepResult]:
        """Execute a workflow and return step results.

        Steps are ordered by dependency graph: steps without depends_on
        run first; dependent steps run after their dependencies complete.
        Parallel steps (those with a `parallel` key) spawn sub-steps
        concurrently.
        """
        self._results = {}
        self._vars = dict(workflow.vars)

        # Build dependency graph: step_id -> list of dependent step dicts
        ready: list[dict] = []
        blocked: list[dict] = []

        for step in workflow.steps:
            deps = step.get("depends_on", [])
            if isinstance(deps, str):
                deps = [deps]
            if not deps:
                ready.append(step)
            else:
                blocked.append(step)

        # Execute ready steps, then unblock dependents
        while ready:
            # Execute all ready steps (respecting parallelism within each)
            tasks = [self._execute_step(step) for step in ready]
            await asyncio.gather(*tasks, return_exceptions=True)

            # Check which blocked steps are now unblocked
            newly_ready: list[dict] = []
            still_blocked: list[dict] = []

            for step in blocked:
                deps = step.get("depends_on", [])
                if isinstance(deps, str):
                    deps = [deps]
                if all(dep in self._results for dep in deps):
                    # Check condition if present
                    cond = step.get("condition", "")
                    if cond and not self._eval_condition(cond):
                        logger.info("Step %s skipped (condition: %s)", step["id"], cond)
                        self._results[step["id"]] = StepResult(
                            step_id=step["id"], success=True,
                            output="Skipped (condition not met)",
                        )
                        continue
                    newly_ready.append(step)
                else:
                    still_blocked.append(step)

            ready = newly_ready
            blocked = still_blocked

        # Report any steps that couldn't be unblocked
        for step in blocked:
            deps = step.get("depends_on", [])
            missing = [d for d in deps if d not in self._results]
            self._results[step["id"]] = StepResult(
                step_id=step["id"], success=False,
                error=f"Unresolved dependencies: {missing}",
            )

        return dict(self._results)

    async def run_file(self, path: str | Path) -> dict[str, StepResult]:
        """Parse and execute a workflow from a YAML file."""
        wf = parse_workflow_file(path)
        return await self.run(wf)

    async def run_text(self, yaml_text: str) -> dict[str, StepResult]:
        """Parse and execute a workflow from a YAML string."""
        wf = parse_workflow(yaml_text)
        return await self.run(wf)

    # ── Step execution ────────────────────────────────────────────────────

    async def _execute_step(self, step: dict) -> None:
        """Execute a single workflow step."""
        step_id = step.get("id", "unknown")
        logger.debug("Executing step: %s", step_id)

        try:
            # ── Parallel sub-steps ──────────────────────────────────────
            if "parallel" in step:
                sub_steps = step["parallel"]
                if not isinstance(sub_steps, list):
                    raise ValueError(f"Step {step_id}: 'parallel' must be a list")

                sub_tasks = []
                for i, sub in enumerate(sub_steps):
                    sub_dict = {"id": f"{step_id}.{i+1}"}
                    if isinstance(sub, str):
                        sub_dict["agent"] = sub
                    else:
                        sub_dict.update(sub)
                    sub_tasks.append(self._execute_step(sub_dict))

                await asyncio.gather(*sub_tasks)

                # Aggregate parallel results
                outputs = []
                all_ok = True
                for i in range(len(sub_steps)):
                    sid = f"{step_id}.{i+1}"
                    r = self._results.get(sid)
                    if r:
                        outputs.append(f"[{sid}]: {r.output[:200]}")
                        if not r.success:
                            all_ok = False
                self._results[step_id] = StepResult(
                    step_id=step_id, success=all_ok,
                    output="\n".join(outputs),
                )
                return

            # ── Tool step ───────────────────────────────────────────────
            if "tool" in step:
                tool_name = step["tool"]
                args = self._resolve_vars(step.get("args", {}))
                result = await self._run_tool(tool_name, args)
                self._results[step_id] = result
                # Store output as variable for downstream steps
                if result.success:
                    self._vars[step_id] = result.output
                return

            # ── Agent step ──────────────────────────────────────────────
            if "agent" in step:
                prompt = self._resolve_vars(step["agent"])
                model = step.get("model", "")
                result = await self._run_agent(prompt, model)
                self._results[step_id] = result
                if result.success:
                    self._vars[step_id] = result.output
                return

            # ── Shell step (shorthand) ──────────────────────────────────
            if "run" in step:
                command = self._resolve_vars(step["run"])
                result = await self._run_tool("run_shell", {"command": command})
                self._results[step_id] = result
                if result.success:
                    self._vars[step_id] = result.output
                return

            # Unknown step type
            self._results[step_id] = StepResult(
                step_id=step_id, success=False,
                error="No 'tool', 'agent', 'run', or 'parallel' key in step",
            )

        except Exception as e:
            logger.exception("Step %s failed", step_id)
            self._results[step_id] = StepResult(
                step_id=step_id, success=False,
                error=str(e),
            )

    # ── Tool execution ────────────────────────────────────────────────────

    async def _run_tool(self, name: str, args: dict) -> StepResult:
        """Execute a tool by name (delegates to the controller's agent)."""
        if self._controller is None:
            return StepResult(
                step_id=name, success=False,
                error="No agent controller available for tool execution",
            )

        try:
            agent = self._controller.agent
            if agent is None:
                return StepResult(
                    step_id=name, success=False,
                    error="Agent not initialized",
                )

            # Delegate to the agent's tool executor directly
            result = await agent._execute_tool(name, args)
            return StepResult(
                step_id=name, success=result.success if hasattr(result, 'success') else True,
                output=str(getattr(result, 'output', result)),
                error=str(getattr(result, 'error', '')),
            )
        except Exception as e:
            return StepResult(step_id=name, success=False, error=str(e))

    # ── Agent execution ───────────────────────────────────────────────────

    async def _run_agent(self, prompt: str, model: str = "") -> StepResult:
        """Run an agent sub-task."""
        if self._controller is None:
            return StepResult(
                step_id="agent", success=False,
                error="No agent controller available",
            )

        try:
            agent = self._controller.agent
            if agent is None:
                return StepResult(step_id="agent", success=False, error="Agent not initialized")

            output = await agent.run(
                task=prompt, stream=False,
                explicit_model=model if model else "",
                reset_context=False,
            )
            return StepResult(
                step_id="agent", success=True,
                output=output or "",
            )
        except Exception as e:
            return StepResult(step_id="agent", success=False, error=str(e))

    # ── Variable resolution ───────────────────────────────────────────────

    def _resolve_vars(self, value: Any) -> Any:
        """Resolve {{ step_id }} and ${{ step_id.output }} placeholders."""
        if isinstance(value, str):
            import re
            # Replace ${{ step_id }} with the step's output
            def _replace(match):
                ref = match.group(1).strip()
                if ref in self._vars:
                    return str(self._vars[ref])
                if ref in self._results:
                    return str(self._results[ref].output)
                return match.group(0)
            return re.sub(r'\$\{\{\s*([^}]+)\s*\}\}', _replace, value)
        if isinstance(value, dict):
            return {k: self._resolve_vars(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._resolve_vars(v) for v in value]
        return value

    def _eval_condition(self, condition: str) -> bool:
        """Evaluate a simple condition string like 'step_id.success == true'."""
        import re
        # Simple pattern: var.field == value
        m = re.match(r'(\w+)\.(\w+)\s*==\s*(.+)', condition.strip())
        if m:
            var_name, field, expected = m.group(1), m.group(2), m.group(3).strip()
            r = self._results.get(var_name)
            if r is None:
                return False
            actual = getattr(r, field, None)
            if expected.lower() in ("true", "yes"):
                return bool(actual)
            if expected.lower() in ("false", "no"):
                return not bool(actual)
            return str(actual) == expected.strip("'\"")
        # Fallback: truthy check on variable
        return bool(self._vars.get(condition.strip()))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _json_dumps(obj: Any) -> str:
    """JSON-dump with surrogate sanitization."""
    from .utils import sanitize_surrogates
    import json as _json
    return _json.dumps(sanitize_surrogates(obj), ensure_ascii=False)
