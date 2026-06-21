"""Token usage tracking and limit visualization."""
import time

class LimitTracker:
    """Track token usage, costs, and limits.

    Distinguishes between *window* (current conversation size) and
    *cumulative* (total API consumption across all turns).
    """

    def __init__(self, max_tokens: int = 1_000_000, model: str = "gpt-4o"):
        self.max_tokens = max_tokens
        self.model = model
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._total_cost = 0.0
        self._tool_calls = 0
        self._max_tool_calls = 30
        self._start_time = time.time()
        self.window_tokens = 0  # current conversation window estimate

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
        """Cumulative tokens consumed across all API calls this session."""
        return self._prompt_tokens + self._completion_tokens

    @property
    def window_pct(self) -> float:
        """Current conversation window as percentage of max."""
        if self.window_tokens <= 0:
            return 0.0
        return min(100.0, (self.window_tokens / self.max_tokens) * 100)

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
        """Compact status line showing window tokens (not cumulative)."""
        w = self.window_tokens or self.total_tokens
        pct = (w / max(self.max_tokens, 1)) * 100
        return (
            f"tokens: {w:,}/{self.max_tokens:,} ({pct:.0f}%) | "
            f"tools: {self._tool_calls} | "
            f"time: {self.elapsed:.0f}s"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Main ClaudeCodeUI
# ═══════════════════════════════════════════════════════════════════════════════
