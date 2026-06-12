# -*- coding: utf-8 -*-
#!/usr/bin/env python3
"""
Run ALL examples sequentially with full color output.

Usage:
    python examples/run_all.py           # Run all
    python examples/run_all.py --quick   # Skip API
    python examples/run_all.py --verbose # Detailed output
"""

import sys, os, time, argparse, subprocess
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Force color on Windows
if sys.platform == "win32":
    try: sys.stdout.reconfigure(encoding="utf-8")
    except: pass
    os.environ.setdefault("FORCE_COLOR", "1")
    os.environ.setdefault("PYTHONUTF8", "1")

from terminal import (
    ok, fail, warn, info, dim, bold, heading, style, print_banner,
    print_ok, print_fail, print_separator, status_line,
)

EXAMPLES = [
    ("01_basic_chat.py",       "Basic Chat",            True,  False),
    ("02_file_operations.py",  "File Operations",       True,  False),
    ("03_code_search.py",      "Code Search & Analysis", True,  False),
    ("04_skill_demo.py",       "Skill Auto-Detection",  False, False),
    ("05_memory_demo.py",      "Memory System",         False, False),
    ("06_fool_proof_demo.py",  "Fool-Proof Safety",     False, False),
    ("07_privilege_mode.py",   "Privilege & Elevation", False, False),
    ("08_task_planner.py",     "Task Planner",          False, False),
    ("09_git_workflow.py",     "Git Workflow",          False, True),
    ("10_full_agent.py",       "Full Agent (API)",      True,  False),
]


def main():
    parser = argparse.ArgumentParser(description="ATA Coder — Example Suite")
    parser.add_argument("--quick", action="store_true", help="Skip API examples")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    args = parser.parse_args()

    from config import get_config
    config = get_config()
    has_api = bool(config.llm.api_key and "sk-" in config.llm.api_key)
    has_git = subprocess.run(["git", "--version"], capture_output=True).returncode == 0

    # ── Banner ──────────────────────────────────────────────────────────
    print_banner("ATA Coder — Example Suite")
    print(f"  API:   {ok('[OK] Available') if has_api else fail('[X] Not set')}")
    print(f"  Git:   {ok('[OK] Available') if has_git else dim('[ ] Not found')}")
    print(f"  Model: {info(config.llm.model)}")
    print()

    passed, failed, skipped = 0, 0, 0
    start_time = time.time()
    examples_dir = Path(__file__).parent

    for filename, desc, needs_api, needs_git in EXAMPLES:
        filepath = examples_dir / filename

        # Skip check
        reason = None
        if needs_api and (not has_api or args.quick):
            reason = "no API" if not has_api else "--quick"
        if needs_git and not has_git:
            reason = "no git"

        if reason:
            print(f"  {warn('[SKIP]')} {dim(f'{filename:<30}')} {dim(f'({reason})')}")
            skipped += 1
            continue

        # Run
        print(f"  {info('[RUN]')}  {bold(filename):<30} {dim(desc)}", end="", flush=True)
        example_start = time.time()

        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["FORCE_COLOR"] = "1"

        try:
            result = subprocess.run(
                [sys.executable, str(filepath)],
                capture_output=not args.verbose,
                text=True, timeout=180,
                cwd=str(Path(__file__).parent.parent), env=env,
            )
            elapsed = time.time() - example_start

            if result.returncode == 0:
                print(f"\r  {ok('[PASS]')} {bold(filename):<30} {dim(f'({elapsed:.1f}s)')}")
                passed += 1
            else:
                print(f"\r  {fail('[FAIL]')} {bold(filename):<30} {dim(f'({elapsed:.1f}s)')}")
                failed += 1
                if not args.verbose and result.stderr:
                    for line in result.stderr.strip().split("\n")[-3:]:
                        print(f"        {dim(line[:120])}")
        except subprocess.TimeoutExpired:
            print(f"\r  {fail('[FAIL]')} {bold(filename):<30} {dim('(timeout)')}")
            failed += 1
        except Exception as e:
            print(f"\r  {fail('[FAIL]')} {bold(filename):<30} {dim(f'({e})')}")
            failed += 1

    total_time = time.time() - start_time

    # ── Summary ─────────────────────────────────────────────────────────
    print()
    print_separator("═")
    parts = []
    if passed: parts.append(ok(f"{passed} passed"))
    if failed: parts.append(fail(f"{failed} failed"))
    if skipped: parts.append(warn(f"{skipped} skipped"))
    print(f"  {bold('Results:')} {'  '.join(parts)}  {dim(f'({total_time:.1f}s)')}")
    print_separator("═")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
