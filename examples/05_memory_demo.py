"""Example 05: Memory System — CRUD with colored output."""
import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent))
import os
if sys.platform == "win32":
    try: sys.stdout.reconfigure(encoding="utf-8")
    except: pass
    os.environ.setdefault("FORCE_COLOR", "1")

from terminal import ok, info, dim, bold, heading, style, print_separator
from memory import get_memory_store, create_memory

def main():
    store = get_memory_store()
    print(f"\n{heading('Memory System')}")
    print_separator()

    for n in ["user-prefs-demo", "project-arch", "api-reference"]:
        store.delete(n)

    # Create memories
    print(f"\n  {bold('Creating:')}")
    for name, mtype, desc in [
        ("user-prefs-demo", "user", "Coding style: Python, type hints, pytest, Black 100"),
        ("project-arch", "project", "Clean Architecture: domain→app→infra→presentation"),
        ("api-reference", "reference", "Stripe API: https://docs.stripe.com/api"),
    ]:
        create_memory(name, desc.split(":")[0], desc, mtype, store)
        print(f"    {ok('[OK]')} [{style(mtype, 'info')}] {bold(name)}")

    # List
    print(f"\n  {bold('Listing:')}")
    for m in store.list_all():
        print(f"    [{style(m.memory_type, 'info')}] {m.name}: {dim(m.description[:60])}")

    # Search
    print(f"\n  {bold('Search:')} 'architecture'")
    for m in store.search("architecture"):
        print(f"    {ok('[HIT]')} [{m.memory_type}] {m.name}")

    # Context for prompt
    ctx = store.get_memory_context()
    print(f"\n  {bold('Prompt context:')} {dim(f'{len(ctx)} chars')}")

    # Cleanup
    for n in ["user-prefs-demo", "project-arch", "api-reference"]:
        store.delete(n)
    print(f"\n  {ok('[DONE]')} Memory system works!")

if __name__ == "__main__":
    main()
