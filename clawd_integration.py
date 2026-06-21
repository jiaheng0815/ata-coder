"""
Clawd Desktop Pet integration for ATA Coder.

Posts lifecycle events to Clawd's local HTTP server so the desktop pet
can react to the agent's state in real-time.

Detection: reads ~/.clawd/runtime.json to find the running Clawd port.
Events are POSTed to http://127.0.0.1:<port>/state as JSON.

Permission bubbles: when Clawd is running, ATA Coder delegates interactive
permission decisions (Y/N/A/D) to Clawd's permission bubble UI.  The HTTP
request blocks until the user clicks a bubble button.

Usage:
    from .clawd_integration import ClawdIntegration, get_clawd
    clawd = get_clawd()
    clawd.start(session_id="...", cwd="...")
    ...
    # Permission check — blocks until user clicks bubble
    decision = clawd.request_permission("run_shell", {"command": "rm -rf /"}, "sid")
    if decision == "allow":
        execute()
    ...
    clawd.session_end()
"""

import json
import logging
import os
import platform
import threading
from pathlib import Path
from typing import Callable
from urllib import request, error as urllib_error

logger = logging.getLogger(__name__)

CLAWD_SERVER_ID = "clawd-on-desk"
CLAWD_RUNTIME_PATH = Path.home() / ".clawd" / "runtime.json"
SERVER_PORTS = [23333, 23334, 23335, 23336, 23337]

# Fire-and-forget events use a short timeout — don't block the agent loop.
ASYNC_POST_TIMEOUT_S = 2.0   # async (background thread) — generous for localhost

# Critical lifecycle events (stop, error) MUST arrive or Clawd stays
# stuck in the "thinking" animation forever.  Use a longer timeout and
# a generous body size limit.  Still synchronous so it arrives before
# SessionEnd, but won't silently drop on a busy Electron main process.
CRITICAL_POST_TIMEOUT_S = 5.0
CRITICAL_POST_MAX_BYTES = 16_384

# Permission requests are blocking — Clawd holds the HTTP connection
# open until the user clicks a bubble button.  Longer timeout as safety
# net in case Clawd crashes or the user walks away.
PERMISSION_TIMEOUT_S = 600.0  # 10 minutes


def _find_clawd_port() -> int | None:
    """Read the Clawd runtime port from ~/.clawd/runtime.json."""
    try:
        if CLAWD_RUNTIME_PATH.exists():
            data = json.loads(CLAWD_RUNTIME_PATH.read_text(encoding="utf-8"))
            port = int(data.get("port", 0))
            if port in SERVER_PORTS:
                return port
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return None


def is_clawd_running() -> bool:
    """Quick check: is Clawd currently running and reachable?"""
    port = _find_clawd_port()
    if not port:
        return False
    try:
        req = request.Request(
            f"http://127.0.0.1:{port}/health",
            method="GET",
        )
        with request.urlopen(req, timeout=0.3) as resp:
            resp.read()
            return resp.status == 200
    except Exception:
        return False


def _get_pid() -> int:
    """Get current process PID."""
    return os.getpid()


def _get_platform_tag() -> str:
    """Short platform tag for Clawd state events."""
    system = platform.system()
    if system == "Windows":
        return "windows"
    elif system == "Darwin":
        return "macos"
    else:
        return "linux"


def _truncate_tool_input(args: dict) -> dict:
    """Truncate large tool input so it fits the Clawd permission bubble."""
    if not args:
        return {}
    out = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 200:
            out[k] = v[:197] + "..."
        elif isinstance(v, dict):
            out[k] = _truncate_tool_input(v)
        elif isinstance(v, list) and len(v) > 20:
            out[k] = v[:20]
        else:
            out[k] = v
    return out


class ClawdIntegration:
    """Fire-and-forget event poster + blocking permission client.

    State events are posted in background threads (non-blocking).
    Permission requests are BLOCKING — they wait for the user to click
    a bubble button in Clawd's UI.
    """

    def __init__(self):
        self._port: int | None = None
        self._session_id: str = ""
        self._cwd: str = ""
        self._enabled: bool = False
        self._lock = threading.Lock()

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def start(self, session_id: str = "", cwd: str = "", title: str = "") -> None:
        """Initialise and post SessionStart."""
        self._port = _find_clawd_port()
        self._enabled = self._port is not None
        self._session_id = session_id
        self._cwd = cwd or os.getcwd()

        if self._enabled:
            logger.debug("Clawd detected on port %d", self._port)
            body: dict = {
                "agent_id": "ata-coder",
                "event": "SessionStart",
                "state": "idle",
                "session_id": self._session_id or "default",
                "cwd": self._cwd,
                "source_pid": _get_pid(),
                "platform": _get_platform_tag(),
            }
            if title:
                first_line = title.strip().split("\n")[0][:80]
                if first_line:
                    body["session_title"] = first_line
            self._post(body)

    def user_prompt(self, prompt: str = "") -> None:
        """Post UserPromptSubmit — the user has sent a new task."""
        if not self._enabled:
            return
        body: dict = {
            "agent_id": "ata-coder",
            "event": "UserPromptSubmit",
            "state": "thinking",
            "session_id": self._session_id or "default",
            "cwd": self._cwd,
            "source_pid": _get_pid(),
            "platform": _get_platform_tag(),
        }
        if prompt:
            first_line = prompt.strip().split("\n")[0][:120]
            body["session_title"] = first_line if first_line else None
        self._post(body)

    def thinking(self) -> None:
        """Post a working state update — model is generating, show pet working."""
        if not self._enabled:
            return
        self._post({
            "agent_id": "ata-coder",
            "event": "Working",
            "state": "working",
            "session_id": self._session_id or "default",
            "cwd": self._cwd,
            "source_pid": _get_pid(),
            "platform": _get_platform_tag(),
        })

    def tool_use(self, tool_name: str = "", tool_input: dict | None = None) -> None:
        """Post PreToolUse — the model is about to execute a tool."""
        if not self._enabled:
            return
        body: dict = {
            "agent_id": "ata-coder",
            "event": "PreToolUse",
            "state": "working",
            "session_id": self._session_id or "default",
            "cwd": self._cwd,
            "source_pid": _get_pid(),
            "platform": _get_platform_tag(),
        }
        if tool_name:
            body["tool_name"] = tool_name
        if tool_input:
            body["tool_input_fingerprint"] = self._fingerprint(tool_input)
        self._post(body)

    def tool_result(self, tool_name: str = "", success: bool = True) -> None:
        """Post PostToolUse or PostToolUseFailure."""
        if not self._enabled:
            return
        event = "PostToolUse" if success else "PostToolUseFailure"
        state = "working" if success else "error"
        body: dict = {
            "agent_id": "ata-coder",
            "event": event,
            "state": state,
            "session_id": self._session_id or "default",
            "cwd": self._cwd,
            "source_pid": _get_pid(),
            "platform": _get_platform_tag(),
        }
        if tool_name:
            body["tool_name"] = tool_name
        self._post(body)

    def stop(self, assistant_output: str = "") -> None:
        """Post Stop — CRITICAL: must arrive or Clawd stays stuck thinking.

        Uses a longer timeout and larger body limit than fire-and-forget
        events.  Logs a warning on failure so the user knows something is
        wrong instead of silently letting the pet animate forever.
        """
        if not self._enabled:
            return
        body: dict = {
            "agent_id": "ata-coder",
            "event": "Stop",
            "state": "attention",
            "session_id": self._session_id or "default",
            "cwd": self._cwd,
            "source_pid": _get_pid(),
            "platform": _get_platform_tag(),
        }
        if assistant_output:
            # Keep body under 16KB: safe for localhost, avoids silent drop
            text = assistant_output.strip()[:2000]
            body["assistant_last_output"] = text
        ok = self._send_one(body, timeout=CRITICAL_POST_TIMEOUT_S,
                            max_bytes=CRITICAL_POST_MAX_BYTES)
        if not ok:
            logger.warning("Clawd Stop event failed to send — pet may stay in thinking state")

    def error(self, message: str = "") -> None:
        """Post StopFailure — CRITICAL: must arrive or Clawd stays stuck."""
        if not self._enabled:
            return
        body: dict = {
            "agent_id": "ata-coder",
            "event": "StopFailure",
            "state": "error",
            "session_id": self._session_id or "default",
            "cwd": self._cwd,
            "source_pid": _get_pid(),
            "platform": _get_platform_tag(),
            "error_present": True,
        }
        if message:
            body["assistant_last_output"] = message[:2000]
        ok = self._send_one(body, timeout=CRITICAL_POST_TIMEOUT_S,
                            max_bytes=CRITICAL_POST_MAX_BYTES)
        if not ok:
            logger.warning("Clawd StopFailure event failed to send")

    def subagent_start(self) -> None:
        """Post SubagentStart — a sub-agent is launching."""
        if not self._enabled:
            return
        self._post({
            "agent_id": "ata-coder",
            "event": "SubagentStart",
            "state": "juggling",
            "session_id": self._session_id or "default",
            "cwd": self._cwd,
            "source_pid": _get_pid(),
            "platform": _get_platform_tag(),
        })

    def subagent_stop(self) -> None:
        """Post SubagentStop — a sub-agent has finished."""
        if not self._enabled:
            return
        self._post({
            "agent_id": "ata-coder",
            "event": "SubagentStop",
            "state": "working",
            "session_id": self._session_id or "default",
            "cwd": self._cwd,
            "source_pid": _get_pid(),
            "platform": _get_platform_tag(),
        })

    def compact(self) -> None:
        """Post PreCompact — context compaction is starting."""
        if not self._enabled:
            return
        self._post({
            "agent_id": "ata-coder",
            "event": "PreCompact",
            "state": "sweeping",
            "session_id": self._session_id or "default",
            "cwd": self._cwd,
            "source_pid": _get_pid(),
            "platform": _get_platform_tag(),
        })

    def session_end(self) -> None:
        """Post SessionEnd — the agent run is complete.

        Sends immediately (fire-and-forget). Clawd's ONESHOT attention
        animation auto-decays — no artificial delay is needed.
        """
        if not self._enabled:
            return
        self._post({
            "agent_id": "ata-coder",
            "event": "SessionEnd",
            "state": "sleeping",
            "session_id": self._session_id or "default",
            "cwd": self._cwd,
            "source_pid": _get_pid(),
            "platform": _get_platform_tag(),
        })

    async def shutdown_async(self) -> None:
        """Called when the agent is shutting down — await the final event."""
        if self._enabled:
            self.session_end()
            # Also send synchronously to guarantee delivery before exit
            self._send_one({
                "agent_id": "ata-coder",
                "event": "SessionEnd",
                "state": "sleeping",
                "session_id": self._session_id or "default",
                "cwd": self._cwd,
                "source_pid": _get_pid(),
                "platform": _get_platform_tag(),
            }, timeout=CRITICAL_POST_TIMEOUT_S, max_bytes=CRITICAL_POST_MAX_BYTES)
        self._enabled = False

    def shutdown(self) -> None:
        """Called when the agent is shutting down (sync fallback)."""
        self.session_end()
        self._enabled = False

    # ── Permission (blocking) ──────────────────────────────────────────────

    def request_permission(
        self,
        tool_name: str,
        arguments: dict | None = None,
        session_id: str = "",
    ) -> str | None:
        """Ask Clawd to show a 4-option permission bubble (Y/N/A/D).

        This is a BLOCKING call — it holds the HTTP connection open
        until the user clicks a bubble button or the request times out.

        Returns:
            "allow"      — user clicked Yes
            "deny"       — user clicked No
            "allow_all"  — user clicked Always (all tools of this category)
            "deny_all"   — user clicked Deny (all tools of this category)
            None         — Clawd not running, connection failed, or timeout
                           (caller should fall back to built-in prompt)

        The Always/Deny-all actions are represented in Clawd's bubble as:
            Y = allow once (this specific tool call)
            N = deny once
            A = allow all (allow this category for the session)
            D = deny all (deny this category for the session)
        """
        port = self._port
        if not port:
            return None

        sid = session_id or self._session_id or "default"
        tool_input = _truncate_tool_input(arguments or {})

        body = json.dumps({
            "agent_id": "ata-coder",
            "session_id": sid,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "cwd": self._cwd,
            "source_pid": _get_pid(),
            "platform": _get_platform_tag(),
        }, ensure_ascii=False).encode("utf-8")

        try:
            req = request.Request(
                f"http://127.0.0.1:{port}/permission",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "x-clawd-server": CLAWD_SERVER_ID,
                },
                method="POST",
            )
            with request.urlopen(req, timeout=PERMISSION_TIMEOUT_S) as resp:
                raw = resp.read().decode("utf-8")
            data = json.loads(raw)

            # Clawd response format:
            # {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
            #   "decision": {"behavior": "allow"|"deny"}}}
            decision = data.get("hookSpecificOutput", {}).get("decision", {})
            behavior = decision.get("behavior", "")

            if behavior == "allow":
                logger.info("Clawd permission: ALLOW %s", tool_name)
                return "allow"
            elif behavior == "deny":
                logger.info("Clawd permission: DENY %s", tool_name)
                return "deny"
            else:
                logger.warning("Clawd permission: unknown behavior=%r", behavior)
                return None

        except urllib_error.URLError as e:
            logger.debug("Clawd permission unavailable: %s", e)
            return None
        except (OSError, ValueError, json.JSONDecodeError) as e:
            logger.debug("Clawd permission error: %s", e)
            return None
        except Exception:
            logger.exception("Clawd permission failed")
            return None

    # ── Internal ───────────────────────────────────────────────────────────

    def _send_one(self, data: dict, timeout: float = ASYNC_POST_TIMEOUT_S,
                  max_bytes: int = 4096) -> bool:
        """POST state JSON to Clawd. Returns True on success.

        *max_bytes* protects against oversized payloads that Clawd's
        Express server would reject.  Default 4KB for fire-and-forget;
        critical events (stop, error) should use 16KB.
        """
        port = self._port
        if not port:
            return False
        try:
            body_bytes = json.dumps(data, ensure_ascii=False).encode("utf-8")
            if len(body_bytes) > max_bytes:
                logger.warning(
                    "Clawd event too large (%d bytes > %d), dropping: event=%s",
                    len(body_bytes), max_bytes, data.get("event", "?"),
                )
                return False
            req = request.Request(
                f"http://127.0.0.1:{port}/state",
                data=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    "x-clawd-server": CLAWD_SERVER_ID,
                },
                method="POST",
            )
            with request.urlopen(req, timeout=timeout) as resp:
                resp.read()  # consume body to release connection
            return True
        except (urllib_error.URLError, OSError, ValueError):
            return False
        except Exception:
            logger.exception("Clawd post failed")
            return False

    async def _post_async(self, data: dict) -> None:
        """POST state JSON to Clawd via asyncio thread pool (fire-and-forget)."""
        if not self._port:
            return
        import asyncio
        try:
            await asyncio.to_thread(self._send_one, data)
        except Exception:
            pass

    def _post(self, data: dict) -> None:
        """POST state JSON to Clawd (fire-and-forget).

        When running inside an asyncio event loop, schedules the HTTP call
        as a background task so the event loop can track it.  Falls back to
        a daemon thread when no loop is active (e.g. during startup).
        """
        if not self._port:
            return
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._post_async(data))
        except RuntimeError:
            # No running event loop — use daemon thread
            t = threading.Thread(target=self._send_one, args=(data,), daemon=True)
            t.start()

    @staticmethod
    def _fingerprint(tool_input: dict) -> str | None:
        """Lightweight input fingerprint for dedup."""
        try:
            import hashlib
            raw = json.dumps(tool_input, sort_keys=True, ensure_ascii=False, default=str)
            return hashlib.sha1(raw.encode()).hexdigest()
        except Exception:
            return None


# ── Module-level singleton ─────────────────────────────────────────────────

_clawd: ClawdIntegration | None = None


def get_clawd() -> ClawdIntegration:
    """Get or create the global ClawdIntegration singleton."""
    global _clawd
    if _clawd is None:
        _clawd = ClawdIntegration()
    return _clawd


# ── Permission callback wrapper ────────────────────────────────────────────


def create_clawd_permission_handler(
    clawd: ClawdIntegration | None = None,
) -> "Callable[[str, dict, str], bool]":
    """Create a permission handler that delegates to Clawd's bubble UI.

    Returns a callable with signature (tool_name, arguments, category) -> bool
    suitable for use as PermissionStore.set_prompt_callback().

    When Clawd is running, the handler sends a blocking POST to /permission
    and returns the user's bubble decision.  When Clawd is unavailable,
    returns None (caller should fall back to the built-in prompt).

    Usage:
        clawd = get_clawd()
        handler = create_clawd_permission_handler(clawd)
        # Wrap with fallback:
        def combined_prompt(tool_name, args, category):
            result = handler(tool_name, args, category)
            if result is not None:
                return result  # Clawd decided
            return builtin_prompt(tool_name, args, category)  # fallback
    """
    c = clawd or get_clawd()

    def _handler(tool_name: str, arguments: dict, category: str) -> bool | None:
        if not c._enabled:
            return None
        decision = c.request_permission(tool_name, arguments)
        if decision is None:
            return None  # connection failed → fall back
        if decision == "allow" or decision == "allow_all":
            return True
        if decision == "deny" or decision == "deny_all":
            return False
        return None

    return _handler
