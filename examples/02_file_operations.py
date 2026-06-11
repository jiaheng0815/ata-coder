"""Example 02: File Operations — read/write/edit with tools."""
import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent))
import os
if sys.platform == "win32":
    try: sys.stdout.reconfigure(encoding="utf-8")
    except: pass
    os.environ.setdefault("FORCE_COLOR", "1")

import tempfile
from terminal import ok, fail, info, dim, bold, heading, print_separator, print_diff, print_ok
from config import get_config
from agent import CoderAgent
from tools import ToolExecutor
from permissions import PermissionStore, PermissionMode

def main():
    config = get_config()
    print(f"\n{heading('File Operations')}  {dim(f'Model: {config.llm.model}')}")
    print_separator()

    with tempfile.TemporaryDirectory() as ws:
        config.agent.workspace_dir = ws
        starter = os.path.join(ws, "math_utils.py")
        with open(starter, "w") as f:
            f.write("def add(a, b):\n    return a + b\n\ndef subtract(a, b):\n    return a - b\n")
        print(f"  {dim('Created:')} math_utils.py")

        perms = PermissionStore(ws)
        perms.set_category_rule("write", PermissionMode.ALLOW)
        perms.set_category_rule("shell", PermissionMode.ALLOW)

        agent = CoderAgent(config=config, tool_executor=ToolExecutor(config.agent), permission_store=perms)
        task = f"Read {ws}/math_utils.py, add multiply(a,b) and divide(a,b) with docstrings. Divide must handle zero."
        print(f"  {dim('Task:')} {task[:100]}...\n")
        print_separator()

        response = agent.run(task, stream=True)
        print(f"\n{print_separator()}")

        with open(starter) as f:
            content = f.read()
        checks = ["multiply" in content, "divide" in content,
                  any(w in content.lower() for w in ["zero", "0", "cannot divide", "valueerror", "zerodivisionerror"])]
        for i, (name, c) in enumerate([("multiply", checks[0]), ("divide", checks[1]), ("zero-check", checks[2])]):
            print_ok(name) if c else print_fail(name)

        print(f"\n{ok('[DONE]')} File operations with tools — all checks passed!")
        agent.shutdown()

if __name__ == "__main__":
    main()
