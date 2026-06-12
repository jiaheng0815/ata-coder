# -*- coding: utf-8 -*-
"""Example 03: Code Search — grep, read, analyze with colored output."""
import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent))
import os
if sys.platform == "win32":
    try: sys.stdout.reconfigure(encoding="utf-8")
    except: pass
    os.environ.setdefault("FORCE_COLOR", "1")

import tempfile
from terminal import ok, fail, info, dim, bold, heading, print_separator, print_ok
from config import get_config
from agent import CoderAgent
from tools import ToolExecutor
from permissions import PermissionStore, PermissionMode

def main():
    config = get_config()
    print(f"\n{heading('Code Search & Analysis')}  {dim(f'Model: {config.llm.model}')}")
    print_separator()

    with tempfile.TemporaryDirectory() as ws:
        config.agent.workspace_dir = ws
        os.makedirs(os.path.join(ws, "src"))
        with open(os.path.join(ws, "src", "models.py"), "w") as f:
            f.write("class User:\n    def __init__(self, n, e, pw):\n        self.pw = pw  # TODO: hash\n    def check(self, pw):\n        return self.pw == pw  # BUG: plaintext\n")
        with open(os.path.join(ws, "src", "handlers.py"), "w") as f:
            f.write("from src.models import User\ndb = {}\ndef login(e, pw):\n    u = db.get(e)\n    return u.check(pw) if u else False\n")

        perms = PermissionStore(ws)
        perms.set_category_rule("write", PermissionMode.ALLOW)
        perms.set_category_rule("shell", PermissionMode.ALLOW)

        from agent_subsystems import AgentSubsystems
        agent = CoderAgent(config=config, tool_executor=ToolExecutor(config.agent),
                           subsystems=AgentSubsystems(permissions=perms))
        task = f"Search {ws}/src for issues. Grep for password, TODO, BUG, plaintext. Read files, list issues with severity."
        print(f"  {dim('Files:')} src/models.py, src/handlers.py")
        print(f"  {dim('Task:')} grep + read + analyze\n")
        print_separator()

        response = agent.run(task, stream=False)
        print(f"\n{print_separator()}")
        print(f"  {ok('[DONE]')} Code search completed!")
        agent.shutdown()

if __name__ == "__main__":
    main()
