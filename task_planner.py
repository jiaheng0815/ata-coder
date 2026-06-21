"""
Task Planner — automatic decomposition of complex requests.

When the user says "Build a REST API with auth, tests, and docs",
the planner breaks it into ordered subtasks:

  1. Set up project structure
  2. Implement auth module
  3. Create API endpoints
  4. Write tests
  5. Add documentation

Features:
- Automatic decomposition via LLM (or pattern-based fallback)
- Dependency ordering
- Progress tracking
- Parallel subtask marking
- Status: pending → in_progress → completed → failed
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Data model
# ═══════════════════════════════════════════════════════════════════════════════

class TaskStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class SubTask:
    id: int
    subject: str
    description: str = ""
    status: TaskStatus = TaskStatus.PENDING
    depends_on: list[int] = field(default_factory=list)
    parallel_ok: bool = False       # Can run in parallel with siblings
    tool_count: int = 0
    started_at: float = 0.0
    completed_at: float = 0.0
    error: str = ""
    result_summary: str = ""

    @property
    def elapsed(self) -> float:
        if self.started_at == 0:
            return 0
        end = self.completed_at if self.completed_at > 0 else time.time()
        return end - self.started_at

    def to_dict(self) -> dict:
        return {
            "id": self.id, "subject": self.subject,
            "description": self.description, "status": self.status.value,
            "depends_on": self.depends_on, "parallel_ok": self.parallel_ok,
        }


@dataclass
class Plan:
    task_id: str
    title: str
    subtasks: list[SubTask] = field(default_factory=list)
    created_at: str = ""

    @property
    def completed(self) -> int:
        return sum(1 for t in self.subtasks if t.status == TaskStatus.COMPLETED)

    @property
    def total(self) -> int:
        return len(self.subtasks)

    @property
    def progress_pct(self) -> float:
        if self.total == 0:
            return 0
        return (self.completed / self.total) * 100

    @property
    def current(self) -> SubTask | None:
        """Get the current in-progress task."""
        for t in self.subtasks:
            if t.status == TaskStatus.IN_PROGRESS:
                return t
        return None

    def next_pending(self) -> SubTask | None:
        """Get the next task that can be started (dependencies satisfied)."""
        for t in self.subtasks:
            if t.status != TaskStatus.PENDING:
                continue
            # Check dependencies
            deps_met = all(
                self._get(d).status == TaskStatus.COMPLETED
                for d in t.depends_on
            )
            if deps_met:
                return t
        return None

    def _get(self, task_id: int) -> SubTask | None:
        for t in self.subtasks:
            if t.id == task_id:
                return t
        return None

    def progress_bar(self, width: int = 30) -> str:
        filled = int(self.progress_pct / 100 * width)
        bar = "█" * filled + "░" * (width - filled)
        return f"[{self.completed}/{self.total}] {bar} {self.progress_pct:.0f}%"

    def to_prompt(self) -> str:
        """Format the plan as a section for the system prompt."""
        lines = [f"## Task Plan: {self.title}"]
        lines.append(f"Progress: {self.progress_bar()}")
        lines.append("")
        for t in self.subtasks:
            icon = {
                TaskStatus.PENDING: "⬜",
                TaskStatus.IN_PROGRESS: "🔄",
                TaskStatus.COMPLETED: "✅",
                TaskStatus.FAILED: "❌",
                TaskStatus.SKIPPED: "⏭️",
            }.get(t.status, "❓")
            deps = f" (depends on: {t.depends_on})" if t.depends_on else ""
            lines.append(f"{icon} {t.subject}{deps}")
            if t.status == TaskStatus.IN_PROGRESS:
                lines.append("   → Currently working on this")
            elif t.status == TaskStatus.FAILED and t.error:
                lines.append(f"   → Failed: {t.error[:100]}")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# Task Planner
# ═══════════════════════════════════════════════════════════════════════════════

class TaskPlanner:
    """
    Decomposes complex user requests into ordered subtasks.

    Uses pattern-based decomposition as the primary method
    (LLM-based decomposition can be added as an enhancement).
    """

    # Keywords that signal complex multi-step tasks
    COMPLEX_MARKERS = [
        "build", "create", "implement", "add", "set up",
        "and also", "with", "including", "that has", "which includes",
        "full stack", "complete", "end-to-end", "entire",
    ]

    def __init__(self):
        self._current_plan: Plan | None = None
        self._plan_history: list[Plan] = []

    # ── Decomposition ───────────────────────────────────────────────────

    def decompose(self, task: str, llm_client=None) -> Plan:
        """
        Break a task into subtasks.

        Args:
            task: The user's task description
            llm_client: Optional LLM client for smart decomposition

        Returns a Plan with ordered subtasks.
        """
        plan_id = time.strftime("%Y%m%d-%H%M%S")

        # If LLM is available, use it for smarter decomposition
        if llm_client:
            subtasks = self._llm_decompose(task, llm_client)
        else:
            subtasks = self._pattern_decompose(task)

        plan = Plan(
            task_id=plan_id,
            title=task[:100],
            subtasks=subtasks,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        self._current_plan = plan
        self._plan_history.append(plan)
        return plan

    def _llm_decompose(self, task: str, llm_client) -> list[SubTask]:
        """Use LLM to intelligently decompose a task."""
        prompt = f"""Break this coding task into ordered subtasks. Return ONLY a JSON array.

Task: {task}

Rules:
- 3-7 subtasks
- Each has: subject (short), description (1 sentence), depends_on (list of IDs that must complete first), parallel_ok (true if can run alongside siblings with no deps)
- Order logically: setup → core → polish → test
- Dependencies: later tasks depend on earlier ones

Format:
[{{"subject":"...", "description":"...", "depends_on":[1], "parallel_ok":false}}, ...]"""

        try:
            response = llm_client.simple_chat(prompt)
            # Extract JSON array from response
            match = re.search(r"\[.*\]", response, re.DOTALL)
            if match:
                data = json.loads(match.group())
                subtasks = []
                for i, item in enumerate(data, 1):
                    subtasks.append(SubTask(
                        id=i,
                        subject=item.get("subject", f"Step {i}"),
                        description=item.get("description", ""),
                        depends_on=item.get("depends_on", []),
                        parallel_ok=item.get("parallel_ok", False),
                    ))
                if subtasks:
                    return subtasks
        except Exception as e:
            logger.warning("LLM decomposition failed: %s, using pattern fallback", e)

        return self._pattern_decompose(task)

    def _pattern_decompose(self, task: str) -> list[SubTask]:
        """Pattern-based task decomposition (no LLM required)."""
        task_lower = task.lower()
        subtasks = []
        task_id = 1

        # Detect common patterns
        patterns = [
            # (keywords in task, subtask subject, description, depends_on)
            (["project", "structure", "setup", "init", "scaffold"],
             "Set up project structure", "Initialize project structure and dependencies", []),
            (["model", "schema", "database", "migration", "entity"],
             "Define data models/schema", "Create database models, schemas, or entity definitions", [1]),
            (["auth", "login", "authentication", "jwt", "oauth", "session"],
             "Implement authentication", "Add user authentication and authorization", [1]),
            (["api", "endpoint", "route", "controller", "handler", "rest"],
             "Create API endpoints", "Implement REST API routes and handlers", [1]),
            (["service", "business logic", "domain"],
             "Implement business logic", "Core service/business logic layer", [2]),
            (["ui", "frontend", "component", "page", "view", "template"],
             "Build UI components", "Create frontend components and views", [1]),
            (["test", "spec", "unit test", "integration test"],
             "Write tests", "Add unit and integration tests", []),
            (["doc", "readme", "documentation", "comment"],
             "Add documentation", "Write documentation and code comments", []),
            (["deploy", "docker", "ci/cd", "pipeline"],
             "Configure deployment", "Set up deployment and CI/CD pipeline", [1]),
            (["error handling", "validation", "logging"],
             "Add error handling & validation", "Implement error handling, input validation, and logging", [1]),
            (["config", "environment", "settings", ".env"],
             "Configure environment", "Set up configuration and environment variables", [1]),
            (["refactor", "clean up", "optimize"],
             "Refactor and optimize", "Clean up code and optimize performance", [1]),
        ]

        matched_ids = set()
        for keywords, subject, desc, deps in patterns:
            if any(kw in task_lower for kw in keywords):
                # Adjust dependency IDs
                adjusted_deps = [d for d in deps if d in matched_ids] if deps else []
                if not deps and task_id > 1:
                    adjusted_deps = [task_id - 1]  # sequential by default

                subtasks.append(SubTask(
                    id=task_id, subject=subject, description=desc,
                    depends_on=adjusted_deps,
                ))
                matched_ids.add(task_id)
                task_id += 1

        # If no patterns matched or only 1 matched, create generic decomposition
        if len(subtasks) < 2:
            if any(m in task_lower for m in ["and", ",", "also", "then", "after"]):
                # Try to split on natural language breaks
                parts = re.split(r",\s*(?:and\s+|then\s+|also\s+)?", task)
                parts = [p.strip() for p in parts if len(p.strip()) > 10]
                for i, part in enumerate(parts[:6], 1):
                    deps = [i - 1] if i > 1 else []
                    subtasks.append(SubTask(
                        id=i, subject=part[:80],
                        depends_on=deps,
                    ))
            else:
                # Simple sequential breakdown
                subtasks = [
                    SubTask(id=1, subject="Analyze requirements", description="Understand what needs to be done"),
                    SubTask(id=2, subject="Implement solution", description="Write the code", depends_on=[1]),
                    SubTask(id=3, subject="Test and verify", description="Run tests and verify correctness", depends_on=[2]),
                ]

        return subtasks

    # ── Plan management ─────────────────────────────────────────────────

    @property
    def current_plan(self) -> Plan | None:
        return self._current_plan

    def start_task(self, task_id: int) -> SubTask | None:
        """Mark a task as in-progress."""
        if not self._current_plan:
            return None
        for t in self._current_plan.subtasks:
            if t.id == task_id and t.status == TaskStatus.PENDING:
                t.status = TaskStatus.IN_PROGRESS
                t.started_at = time.time()
                return t
        return None

    def complete_task(self, task_id: int, result: str = "") -> SubTask | None:
        """Mark a task as completed."""
        if not self._current_plan:
            return None
        for t in self._current_plan.subtasks:
            if t.id == task_id and t.status == TaskStatus.IN_PROGRESS:
                t.status = TaskStatus.COMPLETED
                t.completed_at = time.time()
                t.result_summary = result
                return t
        return None

    def fail_task(self, task_id: int, error: str = "") -> SubTask | None:
        """Mark a task as failed."""
        if not self._current_plan:
            return None
        for t in self._current_plan.subtasks:
            if t.id == task_id:
                t.status = TaskStatus.FAILED
                t.error = error
                return t
        return None

    def skip_task(self, task_id: int) -> SubTask | None:
        """Skip a task."""
        if not self._current_plan:
            return None
        for t in self._current_plan.subtasks:
            if t.id == task_id and t.status == TaskStatus.PENDING:
                t.status = TaskStatus.SKIPPED
                return t
        return None

    def finish_plan(self) -> Plan | None:
        """Archive the current plan."""
        plan = self._current_plan
        self._current_plan = None
        return plan

    def get_status(self) -> str:
        """Get a status string for the UI."""
        if not self._current_plan:
            return "No active plan."
        return self._current_plan.progress_bar()

    def get_context_for_prompt(self) -> str:
        """Get plan context for injection into the system prompt."""
        if not self._current_plan:
            return ""
        return "\n" + self._current_plan.to_prompt()

    def auto_advance(self) -> SubTask | None:
        """Automatically start the next pending task. Returns it or None."""
        if not self._current_plan:
            return None
        # Check if there's already an in-progress task
        current = self._current_plan.current
        if current:
            return current

        next_task = self._current_plan.next_pending()
        if next_task:
            self.start_task(next_task.id)
            return next_task
        return None
