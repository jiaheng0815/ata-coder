"""
Terminal color & formatting — unified across all outputs.

Auto-detects capabilities and provides a single API for colored output.
Works on Windows (via colorama), Linux, macOS (native ANSI).
Falls back gracefully when color is not available.
"""

import os
import sys
from enum import Enum

# ── Try to import color libraries ────────────────────────────────────────────

HAS_RICH = False
HAS_COLORAMA = False

try:
    from rich.console import Console
    from rich.text import Text
    from rich.style import Style
    from rich.theme import Theme
    HAS_RICH = True
except ImportError:
    pass

try:
    from colorama import init, Fore, Back, Style as CStyle
    init()
    HAS_COLORAMA = True
except ImportError:
    pass


# ═══════════════════════════════════════════════════════════════════════════════
# Color registry
# ═══════════════════════════════════════════════════════════════════════════════

class Ansi:
    """ANSI escape codes — always available as fallback."""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    ITALIC = "\033[3m"
    UNDERLINE = "\033[4m"

    # 16-color standard
    BLACK = "\033[30m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"

    # Bright variants
    GRAY = "\033[90m"
    BRIGHT_RED = "\033[91m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_BLUE = "\033[94m"
    BRIGHT_MAGENTA = "\033[95m"
    BRIGHT_CYAN = "\033[96m"
    BRIGHT_WHITE = "\033[97m"


# ═══════════════════════════════════════════════════════════════════════════════
# Semantic color tokens
# ═══════════════════════════════════════════════════════════════════════════════

# Maps semantic names → ANSI codes (can be overridden by Rich theme)
ANSI_THEME = {
    # Status
    "ok":       Ansi.GREEN,
    "fail":     Ansi.RED,
    "warn":     Ansi.YELLOW,
    "info":     Ansi.CYAN,
    "debug":    Ansi.DIM,

    # Severity
    "critical": Ansi.BRIGHT_RED + Ansi.BOLD,
    "danger":   Ansi.BRIGHT_RED,
    "caution":  Ansi.BRIGHT_YELLOW,
    "safe":     Ansi.GREEN,

    # Categories
    "tool":     Ansi.CYAN,
    "file":     Ansi.BLUE,
    "cmd":      Ansi.MAGENTA,
    "model":    Ansi.BRIGHT_MAGENTA,
    "skill":    Ansi.YELLOW,
    "memory":   Ansi.GREEN,
    "git":      Ansi.BRIGHT_RED,

    # UI elements
    "prompt":   Ansi.BRIGHT_CYAN + Ansi.BOLD,
    "heading":  Ansi.BOLD + Ansi.BRIGHT_CYAN,
    "border":   Ansi.GRAY,
    "dim":      Ansi.DIM,
    "bold":     Ansi.BOLD,
    "reset":    Ansi.RESET,

    # Diff
    "diff_add": Ansi.GREEN,
    "diff_del": Ansi.RED,
    "diff_hdr": Ansi.CYAN + Ansi.BOLD,
    "diff_ctx": Ansi.DIM,

    # Tokens / cost
    "token_low":  Ansi.GREEN,
    "token_mid":  Ansi.YELLOW,
    "token_high": Ansi.BRIGHT_RED,
}


# ═══════════════════════════════════════════════════════════════════════════════
# Terminal capabilities
# ═══════════════════════════════════════════════════════════════════════════════

def _detect_color_support() -> bool:
    """Detect if the terminal supports color."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if not sys.stdout.isatty():
        # Check specifically for common CI systems
        if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
            return True
        return False
    if sys.platform == "win32":
        # Windows Terminal, ConEmu, etc. support ANSI
        return "WT_SESSION" in os.environ or os.environ.get("TERM") == "xterm-256color"
    return True


_COLOR_ENABLED = _detect_color_support()


def color_enabled() -> bool:
    return _COLOR_ENABLED


def enable_color():
    global _COLOR_ENABLED
    _COLOR_ENABLED = True


def disable_color():
    global _COLOR_ENABLED
    _COLOR_ENABLED = False


# ═══════════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════════

def style(text: str, token: str = "") -> str:
    """Apply a semantic style to text. Returns the styled string."""
    if not _COLOR_ENABLED:
        return text
    code = ANSI_THEME.get(token, "")
    if not code:
        return text
    return f"{code}{text}{Ansi.RESET}"


def ok(text: str) -> str:       return style(text, "ok")
def fail(text: str) -> str:     return style(text, "fail")
def warn(text: str) -> str:     return style(text, "warn")
def info(text: str) -> str:     return style(text, "info")
def dim(text: str) -> str:      return style(text, "dim")
def bold(text: str) -> str:     return style(text, "bold")
def heading(text: str) -> str:  return style(text, "heading")
def tool(text: str) -> str:     return style(text, "tool")
def file(text: str) -> str:     return style(text, "file")
def cmd(text: str) -> str:      return style(text, "cmd")

def diff_add(text: str) -> str: return style(text, "diff_add")
def diff_del(text: str) -> str: return style(text, "diff_del")
def diff_hdr(text: str) -> str: return style(text, "diff_hdr")

def critical(text: str) -> str: return style(text, "critical")
def danger(text: str) -> str:   return style(text, "danger")
def safe(text: str) -> str:     return style(text, "safe")

def token_bar(pct: float, width: int = 20) -> str:
    """Render a colored token usage bar."""
    if not _COLOR_ENABLED:
        filled = int(pct / 100 * width)
        return "█" * filled + "░" * (width - filled)
    filled = int(pct / 100 * width)
    if pct < 50:
        color = Ansi.GREEN
    elif pct < 80:
        color = Ansi.YELLOW
    else:
        color = Ansi.BRIGHT_RED
    return f"{color}{'█' * filled}{Ansi.DIM}{'░' * (width - filled)}{Ansi.RESET} {pct:.0f}%"


# ═══════════════════════════════════════════════════════════════════════════════
# Rich console (when available)
# ═══════════════════════════════════════════════════════════════════════════════

_rich_console: "Console | None" = None


def get_rich_console() -> "Console | None":
    """Get or create a Rich Console instance."""
    global _rich_console
    if not HAS_RICH:
        return None
    if _rich_console is None:
        _rich_console = Console(force_terminal=_COLOR_ENABLED)
    return _rich_console


def rich_print(*args, **kwargs):
    """Print via Rich if available, else plain print."""
    c = get_rich_console()
    if c:
        c.print(*args, **kwargs)
    else:
        print(*args, **kwargs)


# ═══════════════════════════════════════════════════════════════════════════════
# Convenience printers
# ═══════════════════════════════════════════════════════════════════════════════

def print_ok(msg: str):
    print(f"  {ok('[OK]')} {dim(msg)}")

def print_fail(msg: str):
    print(f"  {fail('[FAIL]')} {msg}")

def print_warn(msg: str):
    print(f"  {warn('[WARN]')} {msg}")

def print_info(msg: str):
    print(f"  {info('[i]')} {dim(msg)}")

def print_tool(name: str, args: str = ""):
    print(f"  {tool(name)} {dim(args)}" if args else f"  {tool(name)}")

def print_file(path: str):
    print(f"  {file(path)}")

def print_heading(text: str):
    print(f"\n{heading(text)}")

def print_separator(char: str = "─", width: int = 60):
    print(dim(char * width))

def print_diff(old: str, new: str, filepath: str = ""):
    """Print a colored unified diff."""
    import difflib
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{filepath}", tofile=f"b/{filepath}",
    )
    for line in diff:
        line = line.rstrip("\n")
        if line.startswith("---") or line.startswith("+++"):
            print(dim(line))
        elif line.startswith("@@"):
            print(diff_hdr(line))
        elif line.startswith("+"):
            print(diff_add(line))
        elif line.startswith("-"):
            print(diff_del(line))
        else:
            print(dim(line))

def print_banner(title: str, width: int = 60):
    """Print a colored banner."""
    print()
    print(bold("╔" + "═" * (width - 2) + "╗"))
    pad = (width - 2 - len(title)) // 2
    print(bold("║") + " " * pad + title + " " * (width - 2 - pad - len(title)) + bold("║"))
    print(bold("╚" + "═" * (width - 2) + "╝"))
    print()


# ═══════════════════════════════════════════════════════════════════════════════
# Status line
# ═══════════════════════════════════════════════════════════════════════════════

def status_line(tokens: int = 0, max_tokens: int = 0, tools: int = 0,
                max_tools: int = 0, cost: float = 0, elapsed: float = 0,
                git_status: str = "", dangerous: bool = False) -> str:
    """Build a colored status line."""
    parts = []
    if dangerous:
        parts.append(danger("[DANGEROUS]"))

    if tokens:
        pct = min(100, tokens / max_tokens * 100) if max_tokens else 0
        color = "token_low" if pct < 50 else ("token_mid" if pct < 80 else "token_high")
        parts.append(f"tokens: {style(f'{tokens:,}/{max_tokens:,}', color)}")
        parts.append(token_bar(pct, 12))

    if tools:
        parts.append(f"tools: {dim(f'{tools}/{max_tools}')}")

    if cost:
        parts.append(f"cost: {ok(f'${cost:.4f}')}")

    if elapsed:
        parts.append(f"time: {dim(f'{elapsed:.0f}s')}")

    if git_status:
        parts.append(f"git: {style(git_status, 'git')}")

    return " | ".join(parts)
