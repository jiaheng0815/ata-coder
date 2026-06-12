"""
Claude Code-level terminal UI with full color scheme, inline diffs,
syntax highlighting, token tracking, and interactive prompts.

Color scheme:
  green  = success, additions, OK
  red    = errors, deletions, FAIL
  yellow = warnings, thinking, skill changes
  cyan   = tool names, user prompt
  blue   = file paths
  magenta = model info, MCP
  dim    = metadata, secondary info
"""

import difflib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

try:
    import readline
    HAS_READLINE = True
except ImportError:
    HAS_READLINE = False

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.styles import Style as PTStyle
    from prompt_toolkit.history import InMemoryHistory
    HAS_PROMPT_TOOLKIT = True
except ImportError:
    HAS_PROMPT_TOOLKIT = False


class _DedupeHistory(InMemoryHistory):
    """Input history that skips consecutive duplicate entries.

    Wraps prompt_toolkit's InMemoryHistory.  Consecutive identical
    inputs are stored only once — pressing ↑ multiple times jumps
    straight to the *different* previous inputs instead of showing
    the same one over and over.
    """

    def __init__(self):
        super().__init__()
        self._last_text: str | None = None

    def append_string(self, string: str) -> None:
        """Store only if different from the immediately preceding entry."""
        s = string.strip()
        if s and s != self._last_text:
            self._last_text = s
            super().append_string(s)


try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.markup import escape as rich_escape
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.text import Text
    from rich.table import Table
    from rich.layout import Layout
    from rich.live import Live
    from rich.spinner import Spinner
    from rich.progress import Progress, BarColumn, TextColumn, SpinnerColumn, TaskProgressColumn
    from rich import box
    from rich.prompt import Prompt, Confirm
    from rich.columns import Columns
    from rich.rule import Rule
    from rich.style import Style
    from rich.theme import Theme
    HAS_RICH = True
except ImportError:
    HAS_RICH = False
    def rich_escape(text: str) -> str:
        return text.replace("[", "\\[").replace("]", "\\]")


# ── One Dark Pro theme ────────────────────────────────────────────────────────

ONE_DARK_THEME = Theme({
    # Base
    "background":      "#282C34",
    "foreground":      "#ABB2BF",
    "black":           "#3F4451",
    "white":           "#D7DAE0",
    # Accent
    "red":             "#E06C75",
    "green":           "#98C379",
    "yellow":          "#D19A66",
    "blue":            "#61AFEF",
    "cyan":            "#56B6C2",
    "purple":          "#C678DD",
    # Bright variants
    "brightBlack":     "#4F5666",
    "brightRed":       "#BE5046",
    "brightGreen":     "#A5E075",
    "brightYellow":    "#E5C07B",
    "brightBlue":      "#4DC4FF",
    "brightCyan":      "#4CD1E0",
    "brightPurple":    "#DE73FF",
    "brightWhite":     "#E6E6E6",
    # Semantic aliases
    "border":          "#3F4451",
    "comment":         "#5C6370",
    "dim":             "#5C6370",
    "prompt":          "#61AFEF",
    "success":         "#98C379",
    "warning":         "#D19A66",
    "error":           "#E06C75",
    "info":            "#56B6C2",
    "cursor":          "#528BFF",
    "selection":       "#ABB2BF",
}) if HAS_RICH else None

ONE_DARK_SYNTAX = {
    "background":       "#282C34",
    "default":          "#ABB2BF",    # variables, punctuation
    "keyword":          "#E06C75",    # if, else, for, while, return, import, class
    "string":           "#E5C07B",    # "strings", 'strings'
    "number":           "#56B6C2",    # 42, 3.14, 0xFF
    "name.function":    "#98C379",    # function_names(), method_calls()
    "name.class":       "#E5C07B",    # ClassName, Interface, Enum
    "name.tag":         "#E06C75",    # <HTML>, </tags>
    "name.attribute":   "#ABB2BF",    # obj.property
    "operator":         "#ABB2BF",    # + - * / = => ===
    "comment":          "#5C6370",    # // /* */
    "comment.line":     "#5C6370",
    "punctuation":      "#ABB2BF",    # ( ) [ ] { } , ; .
    "name.builtin":     "#56B6C2",    # console, Array, parseInt
    "name.constant":    "#56B6C2",    # null, undefined, true, false
} if HAS_RICH else {}

try:
    from colorama import init, Fore, Back, Style as ColoramaStyle
    init()
    HAS_COLORAMA = True
except ImportError:
    HAS_COLORAMA = False

logger = logging.getLogger(__name__)

from .agent import AgentEvent, TextDeltaEvent, ToolCallEvent, ToolResultEvent
from .agent import ThinkingEvent, ErrorEvent, CompleteEvent, SkillChangedEvent, ReasoningEvent


# ═══════════════════════════════════════════════════════════════════════════════
# Color & Icon constants
# ═══════════════════════════════════════════════════════════════════════════════

# Tool → icon mapping (Unicode-safe fallbacks)
TOOL_ICONS = {
    "read_file":   "[cyan]📄[/cyan]" if HAS_RICH else "[read]",
    "write_file":  "[yellow]📝[/yellow]" if HAS_RICH else "[write]",
    "edit_file":   "[yellow]✏️[/yellow]" if HAS_RICH else "[edit]",
    "run_shell":   "[magenta]⚡[/magenta]" if HAS_RICH else "[exec]",
    "grep":        "[blue]🔍[/blue]" if HAS_RICH else "[grep]",
    "glob":        "[blue]🌐[/blue]" if HAS_RICH else "[glob]",
    "list_dir":    "[blue]📂[/blue]" if HAS_RICH else "[ls]",
}

CATEGORY_COLORS = {
    "read":  "blue",
    "write": "yellow",
    "shell": "magenta",
    "mcp":   "green",
}

CATEGORY_LABELS = {
    "read":  "READ",
    "write": "WRITE",
    "shell": "EXEC",
    "mcp":   "MCP",
}

# Severity colors
SEVERITY_STYLES = {
    "critical": "bold white on red",
    "high":     "bold red",
    "medium":   "yellow",
    "low":      "dim",
}


# ═══════════════════════════════════════════════════════════════════════════════
# Fallback color constants (used by render_diff_simple, show_welcome, etc.)
# ═══════════════════════════════════════════════════════════════════════════════

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

class LimitTracker:
    """Track token usage, costs, and limits."""

    def __init__(self, max_tokens: int = 1_000_000, model: str = "gpt-4o"):
        self.max_tokens = max_tokens
        self.model = model
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._total_cost = 0.0
        self._tool_calls = 0
        self._max_tool_calls = 30
        self._start_time = time.time()

        # Price per 1M tokens (input, output) — from centralized model registry
        from .model_registry import get_model_cost
        self._get_cost = get_model_cost  # callable: model_id -> (input_price, output_price)

    def add_usage(self, prompt_tokens: int = 0, completion_tokens: int = 0):
        self._prompt_tokens += prompt_tokens
        self._completion_tokens += completion_tokens
        inp_price, out_price = self._get_cost(self.model)
        cost = (prompt_tokens / 1_000_000) * inp_price + (completion_tokens / 1_000_000) * out_price
        self._total_cost += cost

    def add_tool_call(self):
        self._tool_calls += 1

    @property
    def total_tokens(self) -> int:
        return self._prompt_tokens + self._completion_tokens

    @property
    def usage_pct(self) -> float:
        return min(100.0, (self.total_tokens / self.max_tokens) * 100)

    @property
    def tool_pct(self) -> float:
        return min(100.0, (self._tool_calls / self._max_tool_calls) * 100)

    @property
    def cost(self) -> float:
        return self._total_cost

    @property
    def elapsed(self) -> float:
        return time.time() - self._start_time

    def render_bar(self, pct: float, width: int = 20) -> str:
        """Render a colored progress bar."""
        filled = int(pct / 100 * width)
        if pct < 50:
            color = "green"
        elif pct < 80:
            color = "yellow"
        else:
            color = "red"
        bar = "█" * filled + "░" * (width - filled)
        return f"[{color}]{bar}[/{color}] {pct:.0f}%"

    def status_line(self) -> str:
        """Generate a compact status line."""
        return (
            f"tokens: {self.total_tokens:,}/{self.max_tokens:,} ({self.usage_pct:.0f}%) | "
            f"tools: {self._tool_calls} | "
            f"time: {self.elapsed:.0f}s"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Main ClaudeCodeUI
# ═══════════════════════════════════════════════════════════════════════════════

class ClaudeCodeUI:
    """Full Claude Code-level terminal UI."""

    # Language aliases for syntax detection
    LANG_MAP = {
        "py": "python", "python": "python", "python3": "python",
        "js": "javascript", "javascript": "javascript",
        "ts": "typescript", "typescript": "typescript",
        "jsx": "javascript", "tsx": "typescript",
        "c": "c", "cpp": "cpp", "c++": "cpp", "cxx": "cpp", "h": "cpp", "hpp": "cpp",
        "java": "java", "go": "go", "golang": "go",
        "rs": "rust", "rust": "rust",
        "rb": "ruby", "ruby": "ruby",
        "php": "php", "swift": "swift", "kt": "kotlin", "kotlin": "kotlin",
        "scala": "scala", "clj": "clojure", "clojure": "clojure",
        "hs": "haskell", "haskell": "haskell",
        "html": "html", "css": "css", "scss": "scss", "sass": "sass",
        "sql": "sql", "mysql": "sql", "psql": "sql", "postgresql": "sql",
        "sh": "bash", "bash": "bash", "zsh": "bash", "shell": "bash",
        "yaml": "yaml", "yml": "yaml",
        "json": "json", "xml": "xml", "toml": "toml",
        "dockerfile": "dockerfile", "docker": "dockerfile",
        "makefile": "makefile", "make": "makefile",
        "diff": "diff", "patch": "diff",
        "md": "markdown", "markdown": "markdown",
        "ini": "ini", "cfg": "ini", "conf": "ini",
        "lua": "lua", "r": "r", "dart": "dart",
        "zig": "zig", "elm": "elm", "erlang": "erlang", "ex": "elixir", "elixir": "elixir",
        "tf": "terraform", "hcl": "terraform", "terraform": "terraform",
        "vim": "vim", "nginx": "nginx",
    }

    def __init__(self, workspace: str = ""):
        self.console = Console(theme=ONE_DARK_THEME, color_system="truecolor") if HAS_RICH else None
        self.workspace = workspace
        self._streaming = False
        self._first_text = True
        self._was_reasoning = False
        self._permission_callback: Callable | None = None
        self._last_edit_file: str = ""  # track last edited file for diff
        self._last_edit_old: str = ""   # old content before edit
        self._tool_outputs: dict[str, str] = {}
        self._tracker = LimitTracker()

        # Code block state machine for syntax highlighting
        self._in_code_block = False
        self._code_lang: str = ""
        self._code_buffer: str = ""
        self._text_buffer: str = ""  # text before code block

        # Bold state machine — handles ** that may be split across chunks
        self._in_bold = False
        self._bold_buffer = ""

        # Command completion state
        self._cmd_names: list[str] = []

        # prompt_toolkit session for multi-line input + history
        self._pt_session = None
        self._input_history = None
        if HAS_PROMPT_TOOLKIT:
            # Only add bindings for newline insertion — Enter/submit and
            # up/down history navigation use prompt_toolkit defaults with
            # multiline=False.
            kb = KeyBindings()

            @kb.add("c-j")
            def _on_newline(event):
                """Ctrl+Enter or Ctrl+J inserts a newline."""
                event.app.current_buffer.insert_text("\n")

            @kb.add("escape", "enter")
            def _on_alt_enter(event):
                """Alt+Enter inserts a newline."""
                event.app.current_buffer.insert_text("\n")

            # History with consecutive deduplication
            self._input_history = _DedupeHistory()

            try:
                self._pt_session = PromptSession(
                    key_bindings=kb,
                    history=self._input_history,
                    style=PTStyle.from_dict({
                        "prompt": "#61AFEF bold",
                        "prompt-danger": "#E06C75 bold",
                    }),
                )
            except Exception:
                logger.warning(
                    "prompt_toolkit unavailable, falling back to single-line"
                )
                self._pt_session = None

    # ── Readline command completion ──────────────────────────────────────

    def setup_command_completion(self, commands: list[tuple[str, str]]):
        """Enable Tab completion for slash commands via readline.

        Args:
            commands: List of (name, description) tuples from commands.py.
        """
        self._cmd_names = sorted(set(name for name, _ in commands))

        if not HAS_READLINE:
            return

        def completer(text: str, state: int) -> str | None:
            """readline completer: match /commands on Tab."""
            if not text.startswith("/"):
                return None
            matches = [c for c in self._cmd_names if c.startswith(text)]
            if state < len(matches):
                return matches[state] + " "
            return None

        readline.set_completer(completer)
        readline.parse_and_bind("tab: complete")
        # Show all matches on first Tab (not double-Tab)
        try:
            readline.parse_and_bind("set show-all-if-ambiguous on")
        except Exception:
            pass

    @staticmethod
    def _e(text: str) -> str:
        """Escape Rich markup in external content to prevent MarkupError."""
        return rich_escape(text)

    def _write_text_with_bold(self, text: str):
        """Character-by-character bold state machine.

        Detects ** pairs (even when split across chunks via _bold_buffer)
        and emits ANSI bold-on / bold-off sequences.  Code blocks are
        handled separately — this method is only called for non-code text.
        """
        i = 0
        while i < len(text):
            ch = text[i]

            # ── Flush buffered * from a previous chunk ──────────────
            if self._bold_buffer:
                if ch == '*':
                    # ** completed across chunk boundary
                    self._bold_buffer = ""
                    if self._in_bold:
                        sys.stdout.write(Colors.RESET)
                        self._in_bold = False
                    else:
                        sys.stdout.write(Colors.BOLD)
                        self._in_bold = True
                    i += 1
                    continue
                else:
                    # Lone * — not part of a ** pair
                    sys.stdout.write('*')
                    self._bold_buffer = ""
                    # fall through to handle current ch

            # ── Normal character processing ────────────────────────
            if ch == '*':
                if i + 1 < len(text) and text[i + 1] == '*':
                    # Complete ** pair within this chunk
                    if self._in_bold:
                        sys.stdout.write(Colors.RESET)
                        self._in_bold = False
                    else:
                        sys.stdout.write(Colors.BOLD)
                        self._in_bold = True
                    i += 2
                    continue
                else:
                    # Single * — buffer it, could continue in next chunk
                    self._bold_buffer = '*'
                    i += 1
                    continue
            else:
                sys.stdout.write(ch)
            i += 1

    def _detect_lang(self, fence_info: str) -> str:
        """Detect language from code fence info string."""
        lang = fence_info.strip().lower().split()[0] if fence_info.strip() else ""
        return self.LANG_MAP.get(lang, lang if lang else "text")

    # ── Welcome ──────────────────────────────────────────────────────────

    def show_welcome(self, model: str, workspace: str, skill: str = "",
                     project_info=None, mcp_servers: list[str] | None = None):
        self.workspace = workspace
        self._tracker = LimitTracker(model=model)

        if not HAS_RICH:
            self._simple_welcome(model, workspace, skill, project_info, mcp_servers)
            return

        self.console.print()

        # Big ASCII art title — ATA CODER
        A = "#61AFEF"
        title = [
            f"[bold][{A}]        █████╗  ████████╗  █████╗        ██████╗  ██████╗  ██████╗  ███████╗ ██████╗  [/{A}][/bold]",
            f"[bold][{A}]       ██╔══██╗ ╚══██╔══╝ ██╔══██╗      ██╔════╝ ██╔═══██╗ ██╔══██╗ ██╔════╝ ██╔══██╗ [/{A}][/bold]",
            f"[bold][{A}]       ███████║    ██║    ███████║      ██║      ██║   ██║ ██║  ██║ █████╗   ██████╔╝ [/{A}][/bold]",
            f"[bold][{A}]       ██╔══██║    ██║    ██╔══██║      ██║      ██║   ██║ ██║  ██║ ██╔══╝   ██╔══██╗ [/{A}][/bold]",
            f"[bold][{A}]       ██║  ██║    ██║    ██║  ██║      ╚██████╗ ╚██████╔╝ ██████╔╝ ███████╗ ██║  ██║ [/{A}][/bold]",
            f"[bold][{A}]       ╚═╝  ╚═╝    ╚═╝    ╚═╝  ╚═╝       ╚═════╝  ╚═════╝  ╚═════╝  ╚══════╝ ╚═╝  ╚═╝ [/{A}][/bold]",
        ]

        # Fetch model routing info
        try:
            from .model_router import get_model_info
            mi = get_model_info()
            model_info = f"[dim]strong={mi['opus']}  fast={mi['haiku']}  sub={mi['subagent']}[/dim]"
        except Exception:
            model_info = ""

        # Main welcome panel
        info_lines = [
            "",
            f"[dim]Model:[/dim]  [green]{model}[/green]  {model_info}",
            f"[dim]Workspace:[/dim]  [blue]{workspace}[/blue]",
        ]
        if project_info:
            if project_info.languages:
                info_lines.append(f"[dim]Languages:[/dim]  [blue]{', '.join(project_info.languages)}[/blue]")
            if project_info.is_git_repo:
                info_lines.append(f"[dim]Git:[/dim]  [dim]branch={project_info.git_branch}[/dim]")

        if mcp_servers:
            info_lines.append(f"[dim]MCP:[/dim]  [green]{', '.join(mcp_servers)}[/green]")

        # Add privilege info
        from .privilege import detect_privilege, detect_os, PrivilegeLevel
        priv = detect_privilege()
        os_name = detect_os().value
        if priv == PrivilegeLevel.ROOT:
            info_lines.append(f"[dim]Privilege:[/dim]  [red bold]ROOT ({os_name})[/red bold] [dim]— full system access[/dim]")
        elif priv == PrivilegeLevel.ADMIN:
            info_lines.append(f"[dim]Privilege:[/dim]  [yellow]admin ({os_name})[/yellow] [dim]— /dangerous on to elevate[/dim]")
        else:
            info_lines.append(f"[dim]Privilege:[/dim]  [dim]user ({os_name})[/dim]")

        info_lines.append("")
        info_lines.append("[dim]Type your task or / for commands (Tab to complete). Ctrl+C to interrupt.[/dim]")

        self.console.print(Panel("\n".join(title + info_lines), border_style="#3F4451", padding=(1, 2)))
        self.console.print()

    def _simple_welcome(self, model, workspace, skill, project_info, mcp_servers):
        print(f"\n{Colors.BOLD}{Colors.CYAN}[ATA Coder]{Colors.RESET}")
        print(f"  {Colors.DIM}Model:{Colors.RESET} {Colors.GREEN}{model}{Colors.RESET}")
        print(f"  {Colors.DIM}Workspace:{Colors.RESET} {Colors.BLUE}{workspace}{Colors.RESET}")
        if project_info and project_info.languages:
            print(f"  {Colors.DIM}Project:{Colors.RESET} {', '.join(project_info.languages)}")
        print(f"  {Colors.DIM}Type / for commands (Tab to complete){Colors.RESET}")
        print()

    def reset_stream(self):
        """Clear all streaming state. Call on interrupt/disconnect."""
        self._streaming = False
        self._first_text = True
        self._was_reasoning = False
        self._in_code_block = False
        self._code_buffer = ""
        self._code_lang = ""
        self._text_buffer = ""
        self._in_bold = False
        self._bold_buffer = ""

    # ── Event dispatcher ─────────────────────────────────────────────────

    def on_event(self, event: AgentEvent):
        if isinstance(event, ThinkingEvent):
            pass
        elif isinstance(event, ReasoningEvent):
            self._on_reasoning(event)
        elif isinstance(event, TextDeltaEvent):
            self._on_text(event.text)
        elif isinstance(event, SkillChangedEvent):
            self._on_skill_change(event)
        elif isinstance(event, ToolCallEvent):
            self._on_tool_call(event)
        elif isinstance(event, ToolResultEvent):
            self._on_tool_result(event)
        elif isinstance(event, ErrorEvent):
            self._on_error(event)
        elif isinstance(event, CompleteEvent):
            self._on_complete(event)

    # ── Text streaming with code block detection ────────────────────────

    def _on_text(self, text: str):
        if self._first_text:
            self._first_text = False
            if self._was_reasoning:
                sys.stdout.write("\n\n")
                sys.stdout.flush()
            self._was_reasoning = False

        # Prepend any buffered partial fence from previous chunk
        if self._text_buffer:
            text = self._text_buffer + text
            self._text_buffer = ""

        # Feed text through code block state machine
        while text:
            if not self._in_code_block:
                # Looking for opening ```
                idx = text.find("```")
                if idx == -1:
                    # No fence found — but check for partial fence at end
                    for partial_len in (2, 1):
                        if text.endswith("`" * partial_len) and not text.endswith("`" * (partial_len + 1)):
                            self._text_buffer = text[-partial_len:]
                            self._write_text_with_bold(text[:-partial_len])
                            sys.stdout.flush()
                            return
                    self._write_text_with_bold(text)
                    sys.stdout.flush()
                    break
                # Output text before the code fence (with bold conversion)
                self._write_text_with_bold(text[:idx])
                sys.stdout.flush()
                rest = text[idx + 3:]
                newline_idx = rest.find("\n")
                if newline_idx == -1:
                    # Fence might not be complete yet — buffer it
                    self._text_buffer = text[idx:]
                    sys.stdout.flush()
                    return
                fence_info = rest[:newline_idx]
                self._code_lang = self._detect_lang(fence_info)
                self._code_buffer = ""
                self._in_code_block = True
                text = rest[newline_idx + 1:]
                sys.stdout.write("\n")
                sys.stdout.flush()
            else:
                # Inside code block — looking for closing ```
                # Check both: \n``` (typical) and ``` at start of chunk
                close_idx = text.find("\n```")
                if close_idx == -1 and text.startswith("```"):
                    close_idx = -2  # signal: fence at position 0

                if close_idx == -1:
                    # No closing fence — check for partial at end
                    if text.endswith("`") or text.endswith("``"):
                        cut = 1 if text.endswith("`") and not text.endswith("``") else 2
                        self._code_buffer += text[:-cut]
                        self._text_buffer = text[-cut:]
                    else:
                        self._code_buffer += text
                    break
                elif close_idx == -2:
                    # Closing ``` at very start of chunk
                    self._flush_code_block()
                    self._in_code_block = False
                    self._code_buffer = ""
                    rest = text[3:]  # skip ```
                    if rest.startswith("\n"):
                        rest = rest[1:]
                    text = rest
                else:
                    # Normal case: \n``` found
                    self._code_buffer += text[:close_idx]
                    self._flush_code_block()
                    self._in_code_block = False
                    self._code_buffer = ""
                    rest = text[close_idx + 4:]  # skip \n```
                    if rest.startswith("\n"):
                        rest = rest[1:]
                    text = rest

    def _flush_code_block(self):
        """Render the accumulated code buffer with syntax highlighting.

        ASCII diagrams (box-drawing chars) and plain-text blocks are
        printed raw — no dark background, no syntax highlighting.
        """
        if not self._code_buffer.strip():
            return

        # Detect ASCII diagrams / plain-text blocks
        lang = self._code_lang.lower() if self._code_lang else ""
        is_plain = lang in ("text", "plaintext", "diagram", "tree", "")
        has_box_drawing = any(
            ord(c) >= 0x2500 and ord(c) <= 0x257F  # box-drawing range
            for c in self._code_buffer[:200]
        )

        if is_plain and has_box_drawing:
            # ASCII diagram — print raw, no Syntax background
            self.console.print(self._code_buffer.rstrip())
        elif is_plain:
            # Plain text block — dim, no background
            self.console.print(self._code_buffer.rstrip(), style="dim")
        else:
            try:
                syntax = Syntax(
                    self._code_buffer,
                    self._code_lang if self._code_lang else "text",
                    theme="default",
                    line_numbers=False,
                    word_wrap=False,
                    background_color="#282C34",
                )
                self.console.print(syntax)
            except Exception:
                self.console.print(self._code_buffer, style="dim")

        sys.stdout.write("\n")
        sys.stdout.flush()

    # ── Reasoning / Thinking display ────────────────────────────────────

    def _on_reasoning(self, event: ReasoningEvent):
        """Display the model's thinking process in dimmed text."""
        self._was_reasoning = True
        if not HAS_RICH:
            sys.stdout.write(Colors.DIM)
            self._write_text_with_bold(event.text)
            sys.stdout.write(Colors.RESET)
            sys.stdout.flush()
            return
        sys.stdout.write(Colors.DIM)
        self._write_text_with_bold(event.text)
        sys.stdout.write(Colors.RESET)
        sys.stdout.flush()

    # ── Skill change ────────────────────────────────────────────────────

    def _on_skill_change(self, event: SkillChangedEvent):
        if HAS_RICH:
            self.console.print(f"\n  [yellow][skill] Activated: {event.skill_name}[/yellow]")
        else:
            print(f"\n  {Colors.YELLOW}[skill] {event.skill_name}{Colors.RESET}")

    # ── Tool call ───────────────────────────────────────────────────────

    def _on_tool_call(self, event: ToolCallEvent):
        self._tracker.add_tool_call()

        icon = TOOL_ICONS.get(event.tool_name, "[dim][tool][/dim]" if HAS_RICH else "[tool]")
        cat = "mcp" if event.source == "mcp" else ""
        if not cat:
            from .permissions import tool_category
            cat = tool_category(event.tool_name)
        cat_label = CATEGORY_LABELS.get(cat, cat.upper())
        cat_color = CATEGORY_COLORS.get(cat, "dim")

        args_display = self._fmt_args(event)

        if HAS_RICH:
            self.console.print()
            self.console.print(
                f"  {icon} "
                f"[{cat_color}][{cat_label}][/{cat_color}] "
                f"[bold]{event.tool_name}[/bold] "
                f"[dim]{args_display}[/dim]"
            )
        else:
            print(f"\n  {Colors.DIM}[{cat_label}]{Colors.RESET} {event.tool_name} {Colors.DIM}{args_display}{Colors.RESET}")

    def _fmt_args(self, event: ToolCallEvent) -> str:
        """Format tool arguments for compact single-line display."""
        args = event.arguments
        # Primary argument per tool type
        primary = (
            args.get("file_path") or
            args.get("command") or
            args.get("pattern") or
            args.get("path") or
            args.get("content", "")
        )
        if isinstance(primary, str):
            s = primary.replace("\n", "\\n")[:70]
            if len(primary) > 70:
                s += "..."
            return s
        return ""

    # ── Tool result ─────────────────────────────────────────────────────

    def _on_tool_result(self, event: ToolResultEvent):
        if event.result.success:
            self._tool_ok(event)
        else:
            self._tool_fail(event)

    def _tool_ok(self, event: ToolResultEvent):
        tool_name = event.tool_name
        output = event.result.output

        if HAS_RICH:
            # read_file / grep — show line count
            if tool_name in ("read_file", "grep", "glob", "list_dir"):
                lines = output.count("\n") + 1
                chars = len(output)
                preview = output[:120].replace("\n", " ")
                if len(output) > 120:
                    preview += "..."
                self.console.print(f"  [green][OK][/green] [dim]{lines} lines, {chars:,} chars[/dim]")

            # edit_file — show diff!
            elif tool_name == "edit_file" and self._last_edit_old:
                fp = self._last_edit_file
                # Read new content
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        new_content = f.read()
                    if self._last_edit_old and new_content:
                        render_diff_rich(self.console, self._last_edit_old, new_content, fp)
                except Exception:
                    self.console.print(f"  [green][OK][/green] [dim]File edited[/dim]")
                self._last_edit_old = ""
                self._last_edit_file = ""

            # write_file — show summary
            elif tool_name == "write_file":
                lines = output.count("\n") + 1
                size = len(output)
                file_path = self._e(event.arguments.get("file_path", ""))
                self.console.print(f"  [green][OK][/green] [dim]Wrote {file_path}: {lines} lines, {size:,} bytes[/dim]")

            # run_shell — show exit code
            elif tool_name == "run_shell":
                preview = self._e(output[:200].replace("\n", "\\n"))
                if len(output) > 200:
                    preview += "..."
                self.console.print(f"  [green][OK][/green] [dim]{preview}[/dim]")

            else:
                preview = self._e(output[:120].replace("\n", " "))
                self.console.print(f"  [green][OK][/green] [dim]{preview}[/dim]")
        else:
            preview = output[:120].replace("\n", " ")
            print(f"  {Colors.GREEN}[OK]{Colors.RESET} {Colors.DIM}{preview}{Colors.RESET}")

    def _tool_fail(self, event: ToolResultEvent):
        if HAS_RICH:
            self.console.print(f"  [red][FAIL][/red] [red]{self._e(event.result.error)}[/red]")
        else:
            print(f"  {Colors.RED}[FAIL] {event.result.error}{Colors.RESET}")

    # ── Error ────────────────────────────────────────────────────────────

    def _on_error(self, event: ErrorEvent):
        if HAS_RICH:
            self.console.print(f"\n[red bold]Error:[/red bold] [red]{self._e(event.error)}[/red]")
        else:
            print(f"\n{Colors.RED}Error: {event.error}{Colors.RESET}")

    # ── Complete ─────────────────────────────────────────────────────────

    def _on_complete(self, event: CompleteEvent):
        # Flush any remaining code block
        if self._in_code_block:
            self._flush_code_block()
        self._streaming = False
        self._first_text = True
        self._was_reasoning = False
        self._in_code_block = False
        self._code_buffer = ""
        self._code_lang = ""
        self._text_buffer = ""  # clear partial fence buffer
        self._in_bold = False
        self._bold_buffer = ""
        if HAS_RICH:
            self.console.print()  # newline after streamed text
            self.console.print(
                f"[dim]--- {event.total_tool_calls} tools | "
                f"{self._tracker.total_tokens:,} tokens | "
                f"{event.total_time:.1f}s ---[/dim]"
            )
            sys.stdout.flush()
        else:
            print(f"\n{Colors.DIM}--- {event.total_tool_calls} tools, "
                  f"~{self._tracker.total_tokens:,} tokens, "
                  f"{event.total_time:.1f}s ---{Colors.RESET}", flush=True)

    # ── Permission prompt ────────────────────────────────────────────────

    def permission_prompt(self, tool_name: str, arguments: dict[str, Any],
                          category: str) -> bool:
        """Interactive permission prompt with clear formatting."""
        if HAS_RICH:
            return self._rich_permission(tool_name, arguments, category)
        else:
            return self._simple_permission(tool_name, arguments, category)

    def _rich_permission(self, tool_name, arguments, category) -> bool:
        cat_color = CATEGORY_COLORS.get(category, "yellow")
        cat_label = CATEGORY_LABELS.get(category, category.upper())

        lines = [
            f"[bold {cat_color}][{cat_label}][/bold {cat_color}] [bold]{tool_name}[/bold]",
        ]

        if tool_name == "run_shell" and "command" in arguments:
            cmd = self._e(arguments["command"])
            lines.append("")
            lines.append(f"[yellow bold]$ {cmd}[/yellow bold]")
        elif tool_name in ("write_file", "edit_file") and "file_path" in arguments:
            lines.append(f"[cyan]{self._e(arguments['file_path'])}[/cyan]")
            if tool_name == "edit_file" and "old_string" in arguments:
                old = self._e(arguments["old_string"][:120])
                new = self._e(arguments["new_string"][:120])
                lines.append("")
                lines.append(f"  [red]- {old}[/red]")
                lines.append(f"  [green]+ {new}[/green]")
        else:
            for k, v in arguments.items():
                s = self._e(str(v)[:100])
                lines.append(f"[dim]{k}:[/dim] {s}")

        lines.append("")
        lines.append(
            f"[dim][[bold green]y[/bold green]]es  "
            f"[[bold red]n[/bold red]]o  "
            f"[[bold green]a[/bold green]]llow all {category}  "
            f"[[bold red]d[/bold red]]eny all {category}[/dim]"
        )

        self.console.print()
        self.console.print(Panel("\n".join(lines), border_style=cat_color))

        while True:
            try:
                choice = self.console.input("[bold yellow]?[/bold yellow] ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                return False
            if choice in ("y", "yes", ""):
                return True
            elif choice in ("n", "no"):
                return False
            elif choice == "a":
                if self._permission_callback:
                    self._permission_callback("allow_category", category)
                return True
            elif choice == "d":
                if self._permission_callback:
                    self._permission_callback("deny_category", category)
                return False
            else:
                self.console.print("[red]y/n/a/d[/red]")

    def _simple_permission(self, tool_name, arguments, category) -> bool:
        print(f"\n{Colors.YELLOW}[{category.upper()}] {tool_name}{Colors.RESET}")
        if tool_name == "run_shell" and "command" in arguments:
            print(f"  {Colors.YELLOW}$ {arguments['command']}{Colors.RESET}")
        elif "file_path" in arguments:
            print(f"  {Colors.CYAN}{arguments['file_path']}{Colors.RESET}")
        print(f"  {Colors.DIM}[y]es [n]o [a]llow all {category} [d]eny all {category}{Colors.RESET}")
        while True:
            try:
                choice = input(f"{Colors.YELLOW}?{Colors.RESET} ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                return False
            if choice in ("y", "yes", ""): return True
            elif choice in ("n", "no"): return False
            elif choice == "a":
                if self._permission_callback:
                    self._permission_callback("allow_category", category)
                return True
            elif choice == "d":
                if self._permission_callback:
                    self._permission_callback("deny_category", category)
                return False

    def set_permission_callback(self, callback: Callable) -> None:
        self._permission_callback = callback

    # ── Track edit for diff ──────────────────────────────────────────────

    def track_edit(self, file_path: str, old_content: str):
        """Record old content before an edit for diff display."""
        self._last_edit_file = file_path
        self._last_edit_old = old_content

    def track_usage(self, prompt_tokens: int = 0, completion_tokens: int = 0):
        self._tracker.add_usage(prompt_tokens, completion_tokens)

    # ── Input & Status ──────────────────────────────────────────────────

    def get_input(self, session_info: str = "", dangerous: bool = False) -> str:
        status = self._tracker.status_line() if self._tracker.total_tokens > 0 else ""
        if HAS_RICH:
            self.console.print()  # blank line before prompt
            if dangerous:
                self.console.print("[red bold]DANGEROUS MODE[/red bold] [dim]elevated privileges active[/dim]")
            if status:
                self.console.print(f"[dim]{status}[/dim]")

        if HAS_PROMPT_TOOLKIT:
            return self._get_input_pt(dangerous)
        else:
            return self._get_input_fallback(dangerous)

    def _get_input_pt(self, dangerous: bool = False) -> str:
        """Read input via prompt_toolkit.

        Enter         → submit
        Ctrl+Enter    → insert newline
        Alt+Enter     → insert newline
        Up/Down       → browse input history (consecutive dupes skipped)
        """
        if self._pt_session is None:
            return self._get_input_fallback(dangerous)

        prompt_class = "prompt-danger" if dangerous else "prompt"
        try:
            result = self._pt_session.prompt(
                [("class:" + prompt_class, "❯ ")],
                multiline=False,
            )
            sys.stdout.flush()
            return result.strip()
        except (KeyboardInterrupt, EOFError):
            return ""

    def _get_input_fallback(self, dangerous: bool = False) -> str:
        """Fallback single-line input when prompt_toolkit is unavailable."""
        if HAS_RICH:
            prompt_style = "[bold red]❯[/bold red]" if dangerous else "[bold cyan]❯[/bold cyan]"
            result = self.console.input(prompt_style + " ")
            sys.stdout.flush()
            return result.strip()
        else:
            print()
            if dangerous:
                print(f"{Colors.RED}{Colors.BOLD}[DANGEROUS MODE]{Colors.RESET}")
            prompt_char = f"{Colors.RED}{Colors.BOLD}>{Colors.RESET}" if dangerous else f"{Colors.CYAN}{Colors.BOLD}>{Colors.RESET}"
            result = input(prompt_char + " ")
            return result.strip()

    # ── Help ─────────────────────────────────────────────────────────────

    def show_help(self):
        if HAS_RICH:
            help_text = """
[bold cyan]Slash Commands[/bold cyan]
  [cyan]/help[/cyan]              [dim]Show this help[/dim]
  [cyan]/clear[/cyan]             [dim]Start fresh conversation[/dim]
  [cyan]/compact[/cyan]           [dim]Compact conversation history[/dim]
  [cyan]/context[/cyan]           [dim]Show token usage and limits[/dim]
  [cyan]/cost[/cyan]              [dim]Estimate session cost[/dim]

[bold yellow]Skills[/bold yellow]
  [yellow]/skill [name][/yellow]      [dim]Switch persona[/dim]
  [yellow]/skills[/yellow]            [dim]List all skills[/dim]
  [yellow]/skill-auto on|off[/yellow] [dim]Toggle auto-detection[/dim]

[bold green]Memory[/bold green]
  [green]/remember[/green]          [dim]Save: /remember type/name desc | content[/dim]
  [green]/recall <q>[/green]        [dim]Search memories[/dim]
  [green]/memories[/green]          [dim]List all memories[/dim]
  [green]/forget <name>[/green]     [dim]Delete a memory[/dim]

[bold magenta]Sessions[/bold magenta]
  [magenta]/save [name][/magenta]       [dim]Save current session[/dim]
  [magenta]/sessions[/magenta]          [dim]List saved sessions[/dim]
  [magenta]/resume <id>[/magenta]       [dim]Resume saved session[/dim]
  [magenta]/export <id> [path][/magenta] [dim]Export as markdown[/dim]

[bold red]Safety & Undo[/bold red]
  [red]/undo [n|all][/red]         [dim]Undo last N changes[/dim]
  [red]/redo <change-id>[/red]     [dim]Re-apply reverted change[/dim]
  [red]/changes[/red]              [dim]List all file changes[/dim]
  [red]/diff-changes [n][/red]     [dim]Show diffs of recent changes[/dim]
  [red]/dry-run [on|off][/red]     [dim]Preview mode (no actual changes)[/dim]
  [red]/stats[/red]                [dim]Safety & change statistics[/dim]

[bold red]Dangerous Mode[/bold red]  [dim](OS-aware privilege escalation)[/dim]
  [bold red]/dangerous on[/bold red]        [dim]Enable elevated privileges[/dim]
  [bold red]/dangerous off[/bold red]       [dim]Disable, restore safety[/dim]
  [bold red]/dangerous status[/bold red]    [dim]Current mode & OS info[/dim]
  [bold red]/dangerous audit[/bold red]     [dim]Audit log of privileged ops[/dim]
  [bold red]/elevate[/bold red]             [dim]OS-specific elevation guide[/dim]

[bold cyan]Settings[/bold cyan]
  [cyan]/model <n>[/cyan]      [dim]Change model[/dim]
  [cyan]/workspace <p>[/cyan]  [dim]Change workspace[/dim]
  [cyan]/permissions[/cyan]    [dim]Show permission rules[/dim]
  [cyan]/mcp[/cyan]            [dim]MCP server status[/dim]
  [cyan]/mcp-tools[/cyan]      [dim]List MCP tools[/dim]
  [cyan]/templates[/cyan]      [dim]List prompt templates[/dim]
  [cyan]/template <n>[/cyan]   [dim]Render a template[/dim]

[bold]Tips[/bold]
  - Type [cyan]/[/cyan] then [cyan]Tab[/cyan] to see all commands
  - Be specific: [dim]\"Add type hints to api/handlers.py\"[/dim]
  - Use [cyan]--allow-all[/cyan] to skip permission prompts
  - Use [cyan]--resume[/cyan] to continue a saved session
  - The agent auto-detects your skill from the task
"""
            self.console.print(Panel(help_text, border_style="#3F4451"))
        else:
            print("""
Commands: /help /clear /compact /context /cost
Skills:   /skill /skills /skill-auto
Memory:   /remember /recall /memories /forget
Sessions: /save /sessions /resume /export
Settings: /model /workspace /permissions /mcp /mcp-tools /templates /template
Tip:      Type / then press Tab to auto-complete commands
""")

    # ── Context display ────────────────────────────────────────────────

    def show_context(self, total_messages: int, tool_calls: int, skill: str,
                     model: str, estimated_tokens: int, max_tokens: int):
        if HAS_RICH:
            pct = min(100, int(estimated_tokens / max_tokens * 100))
            bar = self._tracker.render_bar(pct)

            table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
            table.add_column("Key", style="bold dim")
            table.add_column("Value")
            table.add_row("Messages", f"{total_messages}")
            table.add_row("Tool calls", f"{tool_calls}")
            table.add_row("Skill", f"{skill}")
            table.add_row("Model", f"{model}")
            table.add_row("Tokens", f"~{estimated_tokens:,} / {max_tokens:,}")
            table.add_row("Usage", bar)
            table.add_row("Time", f"{self._tracker.elapsed:.0f}s")

            self.console.print()
            self.console.print(Panel(table, title="Context", border_style="#3F4451"))
        else:
            pct = min(100, int(estimated_tokens / max_tokens * 100))
            print(f"\nContext: {total_messages} msgs | {tool_calls} tools | ~{estimated_tokens} tokens ({pct}%)")
            print(f"Time: {self._tracker.elapsed:.0f}s")

    # ── Session list ────────────────────────────────────────────────────

    def show_sessions(self, sessions: list):
        if not sessions:
            print("No saved sessions.")
            return
        if HAS_RICH:
            table = Table(title="Saved Sessions", box=box.SIMPLE)
            table.add_column("ID", style="cyan", max_width=45)
            table.add_column("Msgs", justify="right")
            table.add_column("Tools", justify="right")
            table.add_column("Skill", style="yellow")
            table.add_column("Date")
            for s in sessions[:20]:
                table.add_row(
                    s.id[:45], str(s.message_count), str(s.tool_call_count),
                    s.skill or "-", s.updated[:16] if s.updated else "",
                )
            self.console.print(table)
        else:
            for s in sessions[:20]:
                print(f"  {s.id[:50]} [{s.skill or 'default'}] {s.updated[:16]}")
                print(f"    {s.message_count} msgs, {s.tool_call_count} tools")


# ═══════════════════════════════════════════════════════════════════════════════
# Generic diff utility (for external use)
# ═══════════════════════════════════════════════════════════════════════════════

def generate_unified_diff(old_text: str, new_text: str, filename: str = "file",
                         context_lines: int = 3) -> str:
    """Generate a unified diff string."""
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
        n=context_lines,
    )
    result = "".join(diff)
    return result if result else "(no changes)"
