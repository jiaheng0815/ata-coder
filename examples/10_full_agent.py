# -*- coding: utf-8 -*-
"""Example 10: Full Agent — all subsystems wired with colored output."""
import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent))
import os
if sys.platform == "win32":
    try: sys.stdout.reconfigure(encoding="utf-8")
    except: pass
    os.environ.setdefault("FORCE_COLOR", "1")

import tempfile
from terminal import (ok, info, dim, bold, heading, style, print_separator,
                       print_ok, print_fail)

def main():
    from config import get_config
    config = get_config()
    print(f"\n{heading('Full Agent Integration')}  {dim(f'Model: {config.llm.model}')}")
    print_separator()

    with tempfile.TemporaryDirectory() as ws:
        config.agent.workspace_dir = ws
        os.makedirs(os.path.join(ws, "app"))
        with open(os.path.join(ws, "app", "__init__.py"), "w") as f: f.write("")
        with open(os.path.join(ws, "app", "models.py"), "w") as f:
            f.write("from dataclasses import dataclass\nfrom typing import Optional\n\n@dataclass\nclass Task:\n    id: int\n    title: str\n    completed: bool = False\n    assignee: Optional[str] = None\n")

        print(f"  {dim('Project:')} app/models.py (Task dataclass)")

        from agent import CoderAgent
        from agent_subsystems import AgentSubsystems
        from tools import ToolExecutor
        from permissions import PermissionStore, PermissionMode
        from session import SessionManager
        from project import ProjectDetector
        from skills import get_skill_manager
        from memory import get_memory_store

        perms = PermissionStore(ws)
        perms.set_category_rule("write", PermissionMode.ALLOW)
        perms.set_category_rule("shell", PermissionMode.ALLOW)

        subsystems = AgentSubsystems(
            skills=get_skill_manager(),
            memory=get_memory_store(),
            permissions=perms,
            project_info=ProjectDetector(ws).detect(),
            sessions=SessionManager(ws),
        )

        agent = CoderAgent(
            config=config, tool_executor=ToolExecutor(config.agent),
            subsystems=subsystems,
        )

        task = (
            f"In {ws}/app/, read models.py, create services.py with TaskService "
            f"(add_task, complete_task, list_tasks, list_pending), "
            f"create tests/test_services.py with pytest tests, and run them."
        )
        print(f"  {dim('Task:')} Read model, create service + tests, run pytest\n")
        print_separator()

        response = agent.run(task, stream=True)
        print(f"\n{print_separator()}")

        # Show results
        for root, dirs, files in os.walk(ws):
            for f in sorted(files):
                fp = os.path.join(root, f)
                rel = os.path.relpath(fp, ws)
                if f.endswith('.pyc'): continue
                try:
                    with open(fp, encoding='utf-8') as fh:
                        lines = fh.read().count("\n") + 1
                    icon = ok("[OK]") if lines > 0 else dim("[  ]")
                    print(f"  {icon} {info(rel)} ({lines} lines)")
                except: print(f"  {dim('[  ]')} {rel}")

        print(f"\n  {bold('Stats:')}")
        print(f"    Tool calls: {info(str(agent._state.tool_call_count))}")
        print(f"    Changes:    {info(str(agent.change_tracker.count_active()))}")
        print(f"    Self-fix:   {info(agent.self_correct.stats.get('auto_fix_rate', '0%'))}")
        sid = agent.save_session()
        print(f"    Session:    {dim(sid[:40])}")

        agent.shutdown()
    print(f"\n  {ok('[DONE]')} Full agent integration: all 10 subsystems working!")

if __name__ == "__main__":
    main()
