# -*- coding: utf-8 -*-
"""Example 08: Task Planner — decomposition with colored output."""
import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent))
import os
if sys.platform == "win32":
    try: sys.stdout.reconfigure(encoding="utf-8")
    except: pass
    os.environ.setdefault("FORCE_COLOR", "1")

from terminal import ok, info, dim, bold, heading, style, print_separator

def main():
    print(f"\n{heading('Task Planner — Auto Decomposition')}")
    print_separator()

    from task_planner import TaskPlanner
    planner = TaskPlanner()

    tasks = [
        "Build a REST API with JWT auth and tests",
        "Add type hints and write unit tests",
        "Create a React dashboard with API integration",
        "Set up Docker with CI/CD pipeline",
    ]

    for task in tasks:
        plan = planner.decompose(task)
        print(f"\n  {bold(task)}")
        print(f"  {dim(plan.progress_bar())}")
        for t in plan.subtasks:
            icon = {v: k for k, v in {"[ ]": "pending", "[>]": "in_progress", "[x]": "completed", "[!]": "failed"}.items()}.get(t.status.value, "[ ]")
            deps = f" {dim(f'(depends: {t.depends_on})')}" if t.depends_on else ""
            print(f"    {dim(icon)} #{t.id} {t.subject}{deps}")

    # Simulate execution
    print(f"\n  {bold('Simulated Execution:')}")
    plan = planner.decompose("Build auth system with tests")
    while True:
        t = planner.auto_advance()
        if not t: break
        print(f"    {info('[>]')} #{t.id} {t.subject} ... ", end="", flush=True)
        import time; time.sleep(0.15)
        planner.complete_task(t.id)
        print(ok("[x]"))
    print(f"    {ok(plan.progress_bar())}")

    print(f"\n  {ok('[DONE]')} Task planner works!")

if __name__ == "__main__":
    main()
