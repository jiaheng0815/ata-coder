"""
SSE event helpers — formatting, log sanitization, and event conversion.

Extracted from ``server.py`` as part of the planned split
(target ≤400 lines per module).  These are pure utility functions
with no dependency on the HTTP handler class, so they can be
reused across stream handlers (chat, shell, etc.).
"""

import json
import logging
import re as _re
from typing import Any

from .core import (TextDeltaEvent, ToolCallEvent, ToolResultEvent,
                    ToolStreamEvent, CompleteEvent, ErrorEvent, ReasoningEvent, ThinkingEvent)
from .utils import brief_args, sanitize_surrogates

logger = logging.getLogger(__name__)


def sanitize_log(text: str) -> str:
    """Strip common secret patterns from text before logging.

    Redacts API keys (sk-…, rk-…), Bearer tokens, AWS-style keys,
    and Google API keys.  Null bytes are replaced before regex runs
    to prevent crashes.
    """
    if "\x00" in text:
        text = text.replace("\x00", "")
    text = _re.sub(r'sk-[a-zA-Z0-9_-]{20,}', 'sk-***', text)
    text = _re.sub(r'rk-[a-zA-Z0-9_-]{20,}', 'rk-***', text)
    text = _re.sub(r'Bearer\s+[a-zA-Z0-9._\-]+', 'Bearer ***', text)
    text = _re.sub(r'AKIA[0-9A-Z]{16}', 'AKIA***', text)
    text = _re.sub(r'AIza[0-9A-Za-z\-_]{35}', 'AIza***', text)
    return text


def format_sse_data(evt_type: str, payload: Any) -> str | None:
    """Format an (event_type, payload) tuple into a JSON string for SSE output.

    Returns None when the event type should be silently skipped (e.g.
    ThinkingEvent — internal only, not sent to frontend).
    """
    if evt_type == "text":
        return json.dumps({"type": "text", "text": payload}, ensure_ascii=False)
    elif evt_type == "tool_stream":
        return json.dumps({
            "type": "tool_stream",
            "tool": payload.get("tool", ""),
            "chunk": payload.get("chunk", ""),
        }, ensure_ascii=False)
    elif evt_type == "thinking":
        return json.dumps({"type": "thinking", "text": payload}, ensure_ascii=False)
    elif evt_type == "tool_call":
        args = payload.get("arguments", {})
        args = sanitize_surrogates(args)
        return json.dumps({
            "type": "tool_call",
            "tool": payload["name"],
            "args_summary": brief_args(args),
            "args": args,
        }, ensure_ascii=False)
    elif evt_type == "tool_result":
        return json.dumps({
            "type": "tool_result",
            "tool": payload["name"],
            "ok": payload["success"],
            "output": payload.get("output", ""),
        }, ensure_ascii=False)
    elif evt_type == "error":
        return json.dumps({"type": "error", "error": payload.get("error", "")}, ensure_ascii=False)
    elif evt_type == "complete":
        return json.dumps({
            "type": "complete",
            "tools": payload["tool_calls"],
            "time": payload["time"],
        }, ensure_ascii=False)
    return None


def sse_event_tuple(event: Any):
    """Convert an AgentEvent into an (event_type, payload) tuple, or None if skipped.

    This is the central event router for SSE streaming — every agent event
    passes through here before being serialized by ``format_sse_data()``.
    """
    if isinstance(event, TextDeltaEvent):
        logger.debug("\U0001f4e4 text: %s", sanitize_log(event.text[:120]))
        return ("text", event.text)
    elif isinstance(event, ReasoningEvent):
        logger.debug("\U0001f9e0 thinking: %.100s", sanitize_log(event.text))
        return ("thinking", event.text[:200])
    elif isinstance(event, ThinkingEvent):
        return None
    elif isinstance(event, ToolStreamEvent):
        # Real-time shell output — stream immediately to frontend
        return ("tool_stream", {"tool": event.tool_name, "chunk": event.chunk})
    elif isinstance(event, ToolCallEvent):
        logger.debug("\U0001f527 %s %s", event.tool_name, brief_args(event.arguments))
        return ("tool_call", {"name": event.tool_name, "arguments": event.arguments, "source": event.source})
    elif isinstance(event, ToolResultEvent):
        if event.result.success:
            logger.debug("  ✅ %s", sanitize_log((event.result.output or "")[:100].replace("\n"," ")))
        else:
            logger.debug("  ❌ %s", sanitize_log((event.result.error or "?")[:100]))
        return ("tool_result", {"name": event.tool_name, "success": event.result.success,
                                "output": (event.result.output or "")[:4000], "error": event.result.error})
    elif isinstance(event, ErrorEvent):
        logger.info("\U0001f4a5 ERROR: %s", sanitize_log(event.error))
        return ("error", {"error": event.error})
    elif isinstance(event, CompleteEvent):
        logger.info("\U0001f3c1 Complete — %d tools, %.1fs", event.total_tool_calls, event.total_time)
        return ("complete", {"tool_calls": event.total_tool_calls, "time": event.total_time})
    return None
