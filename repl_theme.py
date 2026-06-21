"""Colors, diff rendering, and escape helpers for the REPL UI."""
from rich.panel import Panel
from rich.text import Text
import difflib

try:
    from colorama import Fore, Style as ColoramaStyle
    HAS_COLORAMA = True
except ImportError:
    HAS_COLORAMA = False


class Colors:
    if HAS_COLORAMA:
        RESET = ColoramaStyle.RESET_ALL
        BOLD = ColoramaStyle.BRIGHT
        DIM = ColoramaStyle.DIM
        CYAN = Fore.CYAN
        GREEN = Fore.GREEN
        YELLOW = Fore.YELLOW
        RED = Fore.RED
        BLUE = Fore.BLUE
        MAGENTA = Fore.MAGENTA
    else:
        RESET = "\033[0m"
        BOLD = "\033[1m"
        DIM = "\033[2m"
        CYAN = "\033[36m"
        GREEN = "\033[32m"
        YELLOW = "\033[33m"
        RED = "\033[31m"
        BLUE = "\033[34m"
        MAGENTA = "\033[35m"


# ═══════════════════════════════════════════════════════════════════════════════
# Diff display engine
# ═══════════════════════════════════════════════════════════════════════════════

def render_diff(old_text: str, new_text: str, file_path: str = "",
                context_lines: int = 3) -> str:
    """Generate a colorized unified diff."""
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)

    diff_lines = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{file_path}" if file_path else "a/old",
        tofile=f"b/{file_path}" if file_path else "b/new",
        n=context_lines,
    ))
    return "".join(diff_lines)


def render_diff_rich(console, old_text: str, new_text: str, file_path: str) -> None:
    """Render a colored diff using Rich syntax highlighting."""
    diff_text = render_diff(old_text, new_text, file_path)

    # Build a rich text with colored lines
    rich_text = Text()
    for line in diff_text.splitlines():
        if line.startswith("---"):
            rich_text.append(line + "\n", style="dim")
        elif line.startswith("+++"):
            rich_text.append(line + "\n", style="dim")
        elif line.startswith("@@"):
            rich_text.append(line + "\n", style="bold cyan")
        elif line.startswith("+"):
            rich_text.append(line + "\n", style="green")
        elif line.startswith("-"):
            rich_text.append(line + "\n", style="red")
        elif line.startswith(" "):
            rich_text.append(line + "\n", style="dim")
        else:
            rich_text.append(line + "\n")

    console.print(Panel(rich_text, title=f"[bold][#61AFEF]Diff: {file_path}[/#61AFEF][/bold]", border_style="#3F4451"))


def render_diff_simple(old_text: str, new_text: str, file_path: str) -> str:
    """Generate a colorized diff for non-Rich terminals."""
    diff_text = render_diff(old_text, new_text, file_path)
    colored = []
    for line in diff_text.splitlines():
        if line.startswith("---") or line.startswith("+++"):
            colored.append(f"{Colors.DIM}{line}{Colors.RESET}")
        elif line.startswith("@@"):
            colored.append(f"{Colors.CYAN}{Colors.BOLD}{line}{Colors.RESET}")
        elif line.startswith("+"):
            colored.append(f"{Colors.GREEN}{line}{Colors.RESET}")
        elif line.startswith("-"):
            colored.append(f"{Colors.RED}{line}{Colors.RESET}")
        else:
            colored.append(f"{Colors.DIM}{line}{Colors.RESET}")
    return "\n".join(colored)


# ═══════════════════════════════════════════════════════════════════════════════
# Token & Limit tracking
# ═══════════════════════════════════════════════════════════════════════════════
