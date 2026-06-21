"""ToolResult — standardised result from tool execution."""


# ── Tool result type ─────────────────────────────────────────────────────────

class ToolResult:
    """Result of executing a tool."""

    def __init__(self, success: bool, output: str, error: str = ""):
        self.success = success
        self.output = output
        self.error = error

    def to_message(self) -> str:
        """Format as a message to the LLM."""
        if self.success:
            return self.output
        return f"Error: {self.error}\n\n{self.output}".strip()

    def to_tool_result(self, tool_call_id: str) -> dict:
        """Format as an OpenAI tool result message."""
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": self.to_message(),
        }
