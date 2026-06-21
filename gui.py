"""
ATA Coder — tkinter Desktop GUI (zero extra dependencies)

Polished, modern interface with:
- Message bubbles with user/agent color distinction
- Real-time streaming via background thread
- Buffered reasoning (single block, not per-token spam)
- Slash-command autocomplete popup
- Dark theme with GitHub-inspired palette
- Smooth auto-scroll

Launch via: ata gui
"""

import logging
import queue
import re
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import simpledialog
from typing import Any
import asyncio

_PKG = str(Path(__file__).parent.resolve())
if _PKG not in sys.path:
    sys.path.insert(0, _PKG)

from .agent import (
    CoderAgent, CompleteEvent, ErrorEvent, ReasoningEvent,
    SkillChangedEvent, TextDeltaEvent, ThinkingEvent,
    ToolCallEvent, ToolResultEvent,
)
from .agent_subsystems import AgentSubsystems
from .commands import get_command_list
from .config import AppConfig, get_config
from .main import __version__
from .permissions import PermissionMode, PermissionStore
from .skills import get_skill_manager
from .tools import ToolExecutor
from .utils import brief_args

logger = logging.getLogger(__name__)

# ── Theme ──────────────────────────────────────────────────────────────────────

T = {
    "bg":        "#0d1117",
    "surface":   "#161b22",
    "overlay":   "#1c2128",
    "border":    "#30363d",
    "fg":        "#c9d1d9",
    "fg_dim":    "#8b949e",
    "muted":     "#484f58",
    "accent":    "#58a6ff",
    "green":     "#3fb950",
    "red":       "#f85149",
    "yellow":    "#d2991d",
    "purple":    "#d2a8ff",
    "teal":      "#7ee787",
}

# ── Fonts ─────────────────────────────────────────────────────────────────────

def _pick_font(size: int, bold: bool = False) -> tuple:
    for name in ("Cascadia Code", "Consolas", "Courier New", "monospace"):
        try:
            tk.Label(font=(name, size)).destroy()
            return (name, size, "bold") if bold else (name, size)
        except Exception:
            continue
    return ("monospace", size, "bold") if bold else ("monospace", size)

FT  = _pick_font(11)
FTB = _pick_font(11, bold=True)
FTS = _pick_font(9)
FTM = _pick_font(10)
FTIB = _pick_font(11, bold=True)  # italic-bold via slant if available


# ═══════════════════════════════════════════════════════════════════════════════

class AtaCoderGUI(tk.Tk):
    """Polished tkinter desktop GUI for ATA Coder."""

    def __init__(self, config: AppConfig | None = None, skill: str = ""):
        super().__init__()
        self._config = config or get_config()
        self._active_skill = skill or "general-coder"
        self._eq: queue.Queue = queue.Queue()
        self._running = False
        self._reasoning_buf = ""
        self._reasoning_line_start = ""
        self._insert_buffer = ""  # for heading detection across streaming chunks

        # ── Subsystems ──────────────────────────────────────────────────
        self._skill_mgr = get_skill_manager()
        self._perms = PermissionStore()
        self._perms.set_category_rule("shell", PermissionMode.ALLOW)
        self._perms.set_category_rule("write", PermissionMode.ALLOW)
        self._tool_exec = ToolExecutor(self._config.agent)
        self._tool_exec.setup_file_cache(
            Path(self._config.agent.workspace_dir) / ".ata_coder" / "files")

        self._agent = CoderAgent(
            config=self._config, tool_executor=self._tool_exec,
            subsystems=AgentSubsystems(skills=self._skill_mgr, permissions=self._perms))
        self._agent._event_queue = self._eq
        self._agent.llm.on_usage(lambda p, c: self.after(0, self._update_tokens))

        self._commands = get_command_list()

        # ── Window ───────────────────────────────────────────────────────
        self.configure(bg=T["bg"])
        self.title(f"ATA Coder — {self._config.llm.model}")
        self.geometry("960x700")
        self.minsize(620, 400)

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_interval = 40
        self.after(self._poll_interval, self._poll)

    # ═══════════════════════════════════════════════════════════════════════
    # UI
    # ═══════════════════════════════════════════════════════════════════════

    def _build_ui(self) -> None:
        # ── Top bar ──────────────────────────────────────────────────────
        top = tk.Frame(self, bg=T["surface"], height=42, highlightthickness=0)
        top.pack(fill=tk.X, side=tk.TOP)
        top.pack_propagate(False)

        tk.Label(top, text="  ⚡ ATA Coder", font=FTB, bg=T["surface"],
                fg=T["accent"]).pack(side=tk.LEFT, pady=4)

        self._model_lbl = tk.Label(top, text=self._config.llm.model[:28],
            font=FTS, bg=T["overlay"], fg=T["fg_dim"], padx=10, pady=3,
            cursor="hand2")
        self._model_lbl.pack(side=tk.RIGHT, padx=(0,12), pady=6)
        self._model_lbl.bind("<Button-1>", lambda e: self._change_model())
        tk.Label(top, text="model", font=FTS, bg=T["surface"],
                fg=T["muted"]).pack(side=tk.RIGHT, padx=(0,4))

        # Accent line
        tk.Frame(self, bg=T["border"], height=1).pack(fill=tk.X, side=tk.TOP)

        # ── Chat ─────────────────────────────────────────────────────────
        cf = tk.Frame(self, bg=T["bg"])
        cf.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4,2))

        self._chat = tk.Text(cf, bg=T["bg"], fg=T["fg"], font=FT,
            wrap=tk.WORD, state=tk.DISABLED, relief=tk.FLAT,
            borderwidth=0, padx=20, pady=12, cursor="arrow",
            yscrollcommand=lambda *a: self._sb.set(*a),
            selectbackground=T["border"], selectforeground=T["fg"])
        self._chat.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._sb = tk.Scrollbar(cf, bg=T["surface"], troughcolor=T["bg"],
            activebackground=T["muted"], command=self._chat.yview, width=7,
            borderwidth=0, highlightthickness=0)
        self._sb.pack(side=tk.RIGHT, fill=tk.Y)

        # Tags
        c = self._chat.tag_configure
        c("user",    foreground=T["accent"],    font=FTB,   lmargin1=0, lmargin2=0, spacing1=14, spacing3=6)
        c("agent",   foreground=T["fg"],        font=FT,    lmargin1=0, lmargin2=0, spacing1=0,  spacing3=0)
        c("think",   foreground=T["teal"],      font=FT,    lmargin1=28,lmargin2=0, spacing1=6,  spacing3=2, background=T["surface"])
        c("tool_h",  foreground=T["purple"],    font=FTS,   lmargin1=32,lmargin2=0, spacing1=8,  spacing3=0)
        c("tool_ok", foreground=T["green"],     font=FTS,   lmargin1=44,lmargin2=0, spacing1=0,  spacing3=2)
        c("tool_er", foreground=T["red"],       font=FTS,   lmargin1=44,lmargin2=0, spacing1=0,  spacing3=2)
        c("error",   foreground=T["red"],       font=FTB,   lmargin1=0, lmargin2=0, spacing1=8,  spacing3=6)
        c("sep",     foreground=T["muted"],     font=FTS,   lmargin1=0, lmargin2=0, spacing1=8,  spacing3=6)
        c("status",  foreground=T["muted"],     font=FTS,   lmargin1=0, lmargin2=0, spacing1=4,  spacing3=2)
        c("heading", foreground=T["fg"],        font=FTB,   lmargin1=0, lmargin2=0, spacing1=8,  spacing3=2)

        # ── Input ────────────────────────────────────────────────────────
        inf = tk.Frame(self, bg=T["surface"], highlightthickness=0)
        inf.pack(fill=tk.X, side=tk.BOTTOM, padx=8, pady=(0,8))
        tk.Frame(inf, bg=T["border"], height=1).pack(fill=tk.X, side=tk.TOP)

        row = tk.Frame(inf, bg=T["surface"])
        row.pack(fill=tk.X, padx=10, pady=8)

        self._input = tk.Entry(row, bg=T["overlay"], fg=T["fg"], font=FT,
            relief=tk.FLAT, insertbackground=T["fg"], insertwidth=2,
            borderwidth=0, highlightthickness=0)
        self._input.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(6,8), ipady=3)
        self._input.bind("<Return>", self._on_send)
        self._input.bind("<KeyRelease>", self._on_key)
        self._input.focus_set()

        self._send_btn = tk.Label(row, text="  Send  ", font=FTB,
            bg=T["green"], fg="#ffffff", padx=16, pady=5, cursor="hand2")
        self._send_btn.pack(side=tk.RIGHT)
        self._send_btn.bind("<Button-1>", lambda e: self._on_send())

        # ── Command popup ────────────────────────────────────────────────
        self._popup: tk.Toplevel | None = None
        self._popup_lb: tk.Listbox | None = None
        self._popup_debounce_id: str = ""

        # ── Status bar ───────────────────────────────────────────────────
        sf = tk.Frame(self, bg=T["surface"], height=26, highlightthickness=0)
        sf.pack(fill=tk.X, side=tk.BOTTOM)
        sf.pack_propagate(False)
        tk.Frame(sf, bg=T["border"], height=1).pack(fill=tk.X, side=tk.TOP)

        self._status_tok = tk.Label(sf, text="tokens: ~0", font=FTS,
            bg=T["surface"], fg=T["muted"])
        self._status_tok.pack(side=tk.RIGHT, padx=(0,12))

        self._status_sk = tk.Label(sf, text=f"skill: {self._active_skill}",
            font=FTS, bg=T["surface"], fg=T["muted"])
        self._status_sk.pack(side=tk.RIGHT, padx=(0,16))

        self._status_lbl = tk.Label(sf, text="● Ready", font=FTS,
            bg=T["surface"], fg=T["green"])
        self._status_lbl.pack(side=tk.LEFT, padx=(10,0))

        # ── Welcome ──────────────────────────────────────────────────────
        self._append(f"ATA Coder v{__version__}", "user")
        self._append(f"Model     {self._config.llm.model}", "status")
        self._append(f"Workspace  {self._config.agent.workspace_dir}", "status")
        self._append("Type  /  for commands, or just start chatting.", "status")
        self._append("", "status")

    # ═══════════════════════════════════════════════════════════════════════
    # Chat display helpers
    # ═══════════════════════════════════════════════════════════════════════

    def _append(self, text: str, tag: str = "agent") -> str:
        self._chat.configure(state=tk.NORMAL)
        idx = self._chat.index(tk.END + "-1c")
        self._chat.insert(tk.END, text + "\n", tag)
        self._chat.configure(state=tk.DISABLED)
        self._chat.see(tk.END)
        return idx

    def _insert(self, text: str, tag: str = "agent") -> None:
        self._chat.configure(state=tk.NORMAL)
        self._chat.insert(tk.END, text, tag)
        self._chat.configure(state=tk.DISABLED)
        self._chat.see(tk.END)

    def _insert_streaming(self, text: str) -> None:
        """Insert streaming agent text with ### heading detection.
        Buffers partial lines across chunks so headings split mid-prefix
        are still detected correctly.
        """
        self._insert_buffer += text
        lines = self._insert_buffer.split('\n')
        self._insert_buffer = lines.pop()  # keep incomplete last line

        self._chat.configure(state=tk.NORMAL)
        for line in lines:
            m = re.match(r'^(#{1,3}) (.+)', line)
            if m:
                self._chat.insert(tk.END, m.group(2) + '\n', "heading")
            else:
                self._chat.insert(tk.END, line + '\n', "agent")
        self._chat.configure(state=tk.DISABLED)
        self._chat.see(tk.END)

    def _flush_insert_buffer(self) -> None:
        """Flush any remaining partial line in the insert buffer."""
        if self._insert_buffer:
            self._chat.configure(state=tk.NORMAL)
            m = re.match(r'^(#{1,3}) (.+)', self._insert_buffer)
            if m:
                self._chat.insert(tk.END, m.group(2) + '\n', "heading")
            else:
                self._chat.insert(tk.END, self._insert_buffer + '\n', "agent")
            self._chat.configure(state=tk.DISABLED)
            self._chat.see(tk.END)
            self._insert_buffer = ""

    def _replace_range(self, start: str, end: str, text: str, tag: str) -> None:
        self._chat.configure(state=tk.NORMAL)
        self._chat.delete(start, end)
        self._chat.insert(start, text, tag)
        self._chat.configure(state=tk.DISABLED)
        self._chat.see(tk.END)

    # ═══════════════════════════════════════════════════════════════════════
    # Reasoning buffer
    # ═══════════════════════════════════════════════════════════════════════

    def _reasoning_show(self, text: str = "", *, final: bool = False) -> None:
        """Accumulate and display the model's reasoning in a dimmed block.

        Call with each chunk; call with final=True to flush on tool call / completion.
        """
        if text:
            if not self._reasoning_buf:
                self._reasoning_line_start = self._chat.index(tk.END + "-1c")
            self._reasoning_buf += text
        if not self._reasoning_buf:
            return
        # Truncate to roughly fit the chat pane width (monospace, ~8px/char)
        try:
            width = self._chat.winfo_width()
        except Exception:
            width = 0
        if not isinstance(width, int) or width < 200:
            width = 960
        max_chars = max(60, (width - 80) // 8)
        content = self._reasoning_buf.strip()
        if len(content) > max_chars:
            content = content[:max_chars] + "…"
        self._replace_range(self._reasoning_line_start, tk.END,
                           f"  ▸ {content}\n", "think")
        if final:
            self._reasoning_buf = ""
            self._reasoning_line_start = ""

    # ═══════════════════════════════════════════════════════════════════════
    # Command popup
    # ═══════════════════════════════════════════════════════════════════════

    def _show_popup(self, filter_text: str = "") -> None:
        matches = [(n, d) for n, d in self._commands if n.startswith(filter_text)]
        if not matches:
            self._hide_popup()
            return

        if self._popup is None:
            self._popup = tk.Toplevel(self)
            self._popup.wm_overrideredirect(True)
            self._popup.configure(bg=T["overlay"], highlightbackground=T["border"],
                                 highlightthickness=1)
            self._popup_lb = tk.Listbox(self._popup, bg=T["overlay"], fg=T["fg"],
                font=FTM, relief=tk.FLAT, selectbackground="#1f6feb33",
                selectforeground=T["accent"], activestyle="none",
                borderwidth=0, highlightthickness=0)
            self._popup_lb.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
            self._popup_lb.bind("<ButtonRelease-1>", self._on_popup_select)
            self._popup_lb.bind("<Return>", self._on_popup_select)
            self._popup_lb.bind("<Escape>", lambda e: self._hide_popup())

        self._popup_lb.delete(0, tk.END)
        max_w = 0
        for name, desc in matches:
            line = f"  {name:<22}  {desc}"
            self._popup_lb.insert(tk.END, line)
            if len(line) > max_w:
                max_w = len(line)
        self._popup_lb.configure(height=min(len(matches), 12), width=max_w + 2)
        if matches:
            self._popup_lb.selection_set(0)

        x = self._input.winfo_rootx()
        y = self._input.winfo_rooty() - (min(len(matches), 12) * 20 + 12)
        self._popup.geometry(f"+{x}+{y}")
        self._popup.deiconify()
        self._popup.lift()

    def _hide_popup(self) -> None:
        if self._popup:
            self._popup.withdraw()
        self._input.focus_set()

    def _on_popup_select(self, event: tk.Event | None = None) -> None:
        if self._popup_lb is None:
            return
        sel = self._popup_lb.curselection()
        if sel:
            cmd_name = self._popup_lb.get(sel[0]).strip().split()[0]
            self._input.delete(0, tk.END)
            self._input.insert(0, cmd_name + " ")
            self._input.icursor(tk.END)
        self._hide_popup()

    def _on_key(self, event: tk.Event) -> None:
        text = self._input.get()
        if text.startswith("/"):
            self._debounce_popup(text)
        else:
            self._hide_popup()

    def _debounce_popup(self, filter_text: str) -> None:
        """Schedule popup refresh 150ms after last keystroke."""
        if self._popup_debounce_id:
            self.after_cancel(self._popup_debounce_id)
        self._popup_debounce_id = self.after(150, lambda: self._show_popup(filter_text))

    # ═══════════════════════════════════════════════════════════════════════
    # Event poll
    # ═══════════════════════════════════════════════════════════════════════

    def _poll(self) -> None:
        try:
            while True:
                self._dispatch(self._eq.get_nowait())
        except queue.Empty:
            pass
        self.after(self._poll_interval, self._poll)

    def _dispatch(self, ev: Any) -> None:
        if isinstance(ev, TextDeltaEvent):
            self._reasoning_show(final=True)
            self._insert_streaming(ev.text)

        elif isinstance(ev, ReasoningEvent):
            self._reasoning_show(ev.text)

        elif isinstance(ev, ThinkingEvent):
            self._status_lbl.configure(text="● Thinking…", fg=T["yellow"])

        elif isinstance(ev, ToolCallEvent):
            self._reasoning_show(final=True)
            self._flush_insert_buffer()
            icon = {"builtin": "◆", "mcp": "◇"}.get(ev.source, "○")
            args = brief_args(ev.arguments)
            self._append(f"  {icon} {ev.tool_name}  {args}", "tool_h")
            self._status_lbl.configure(text=f"● {ev.tool_name}", fg=T["purple"])

        elif isinstance(ev, ToolResultEvent):
            if ev.result.success:
                out = (ev.result.output or "").replace("\n", " ")[:150]
                self._append(f"    ✓  {out}", "tool_ok")
            else:
                err = (ev.result.error or "unknown error")[:150]
                self._append(f"    ✗  {err}", "tool_er")

        elif isinstance(ev, SkillChangedEvent):
            self._active_skill = ev.skill_name
            self._status_sk.configure(text=f"skill: {ev.skill_name}")

        elif isinstance(ev, ErrorEvent):
            self._reasoning_show(final=True)
            self._flush_insert_buffer()
            self._append(f"● {ev.error}", "error")
            self._status_lbl.configure(text="● Error", fg=T["red"])

        elif isinstance(ev, CompleteEvent):
            self._reasoning_show(final=True)
            self._flush_insert_buffer()
            self._running = False
            self._status_lbl.configure(text="● Ready", fg=T["green"])
            self._send_btn.configure(text="  Send  ", bg=T["green"], fg="#ffffff")
            self._append(f"── {ev.total_tool_calls} tools · {ev.total_time:.1f}s ──", "sep")

    # ═══════════════════════════════════════════════════════════════════════
    # Actions
    # ═══════════════════════════════════════════════════════════════════════

    def _on_send(self, event: tk.Event | None = None) -> None:
        self._hide_popup()
        if self._running:
            return
        text = self._input.get().strip()
        if not text:
            return
        self._input.delete(0, tk.END)
        self._running = True

        self._append("", "status")
        self._append(f"You  {text}", "user")

        self._send_btn.configure(text="  …  ", bg=T["surface"], fg=T["muted"])
        self._status_lbl.configure(text="● Thinking…", fg=T["yellow"])

        threading.Thread(target=self._run, args=(text,), daemon=True).start()

    def _run(self, task: str) -> None:
        try:
            asyncio.run(self._agent.run(task, stream=True, skill_name=self._active_skill or None))
        except Exception as e:
            self._eq.put(ErrorEvent(f"Agent error: {e}"))
            self._eq.put(CompleteEvent(0, 0))

    def _change_model(self) -> None:
        cur = self._config.llm.model
        m = simpledialog.askstring("Change Model", "Model name:", initialvalue=cur)
        if m and m.strip() and m.strip() != cur:
            m = m.strip()
            self._config.llm.model = m
            self._agent.llm.set_model(m)
            self._agent.llm.register_tools(self._agent._all_tools)
            self.title(f"ATA Coder — {m}")
            self._model_lbl.configure(text=m[:28])
            self._append(f"  ↻  Model → {m}", "status")

    def _update_tokens(self) -> None:
        total = self._agent.llm.total_tokens
        self._status_tok.configure(text=f"tokens: ~{total:,}")

    def _on_close(self) -> None:
        try:
            asyncio.run(self._agent.shutdown())
            self._tool_exec.clear_file_cache()
        except Exception:
            pass
        self.destroy()


# ── Entry ──────────────────────────────────────────────────────────────────────

def launch_gui(config: AppConfig | None = None, skill: str = "") -> None:
    AtaCoderGUI(config=config, skill=skill).mainloop()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    launch_gui()


if __name__ == "__main__":
    main()
