# -*- coding: utf-8 -*-
"""Example 09: Git Workflow — auto-commit, branch, undo with colored output."""
import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent))
import os
if sys.platform == "win32":
    try: sys.stdout.reconfigure(encoding="utf-8")
    except: pass
    os.environ.setdefault("FORCE_COLOR", "1")

import tempfile, subprocess
from terminal import ok, fail, info, dim, bold, heading, print_separator

def main():
    print(f"\n{heading('Git Workflow')}")
    print_separator()

    from git_workflow import GitWorkflow

    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["git", "init", "-q"], cwd=td)
        subprocess.run(["git", "config", "user.email", "demo@test.com"], cwd=td)
        subprocess.run(["git", "config", "user.name", "Demo"], cwd=td)
        with open(os.path.join(td, "README.md"), "w") as f: f.write("# Demo\n")
        subprocess.run(["git", "add", "-A"], cwd=td)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=td)

        git = GitWorkflow(td)
        s = git.get_status()
        print(f"\n  {bold('Status:')} {dim(s.summary())}  branch={info(s.branch)}  clean={ok(str(s.clean))}")

        # Create files
        with open(os.path.join(td, "main.py"), "w") as f: f.write("def hello(): return 'hi'\n")
        with open(os.path.join(td, "test.py"), "w") as f: f.write("def test(): pass\n")
        s = git.get_status()
        print(f"  {bold('After create:')} {dim(s.summary())}")

        # Auto-commit
        ok_c, msg = git.commit()
        print(f"  {bold('Auto-commit:')} {ok('[OK]') if ok_c else fail('[X]')} {dim(msg[:60])}")
        s = git.get_status()
        print(f"  {bold('After commit:')} {dim(s.summary())}  last={info(s.last_commit)}")

        # Branch
        ok_b, msg = git.create_branch("feat/greeting")
        print(f"  {bold('Branch:')} {ok('[OK]') if ok_b else fail('[X]')} {info(msg)}")
        git.switch_branch("main")

        # Undo
        with open(os.path.join(td, "main.py"), "a") as f: f.write("def bye(): return 'bye'\n")
        git.commit("add bye")
        ok_u, msg = git.undo_commit()
        print(f"  {bold('Undo:')} {ok('[OK]') if ok_u else fail('[X]')} {dim(msg)}")

        # Safety
        safe, reason = git.pre_operation_check()
        print(f"  {bold('Safety:')} {ok('Safe') if safe else fail(reason)}")

        print(f"  {bold('Session:')} {dim(git.session_summary()[:100])}")

    print(f"\n  {ok('[DONE]')} Git workflow: commit, branch, undo, stash all working!")

if __name__ == "__main__":
    main()
