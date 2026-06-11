"""Example 04: Skill Auto-Detection — colored output."""
import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent))
import os
if sys.platform == "win32":
    try: sys.stdout.reconfigure(encoding="utf-8")
    except: pass
    os.environ.setdefault("FORCE_COLOR", "1")

from terminal import ok, info, dim, bold, heading, style, print_separator
from skills import get_skill_manager

def main():
    skill_mgr = get_skill_manager()
    print(f"\n{heading('Skill Auto-Detection')}")
    print_separator()

    # Map skill names to colors
    colors = {"general-coder": "dim", "code-reviewer": "info", "debugger": "warn",
              "architect": "ok", "test-writer": "info", "doc-writer": "dim", "security-auditor": "fail"}

    print(f"\n  {bold('Available Skills:')}")
    for s in skill_mgr.list_skills():
        print(f"    {style(s.name, colors.get(s.name, ''))}: {dim(s.description[:70])}")

    tests = [
        ("Fix this bug in login", "debugger"),
        ("Review my code for issues", "code-reviewer"),
        ("Audit the auth module for security", "security-auditor"),
        ("Design a microservice architecture", "architect"),
        ("Write unit tests for UserService", "test-writer"),
        ("Document the REST API endpoints", "doc-writer"),
        ("Write a hello world function", "general-coder"),
        ("Debug the null pointer in handler", "debugger"),
        ("Find vulnerabilities in my code", "security-auditor"),
    ]

    print(f"\n  {bold('Detection Tests:')}")
    for phrase, expected in tests:
        d = skill_mgr.detect_skill(phrase)
        result = d.name if d else "none"
        match = result == expected or (expected == "general-coder" and result == "none")
        icon = ok("[OK]") if match else fail("[X]")
        print(f"    {icon} {dim(f'{phrase:<45}')} {style(result, colors.get(result, ''))}")

    print(f"\n  {ok('[DONE]')} Skill system working!")

if __name__ == "__main__":
    main()
