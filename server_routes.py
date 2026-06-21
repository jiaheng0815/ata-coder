"""
REST endpoint handlers — health, tools, skills, models, sessions, static files,
and interactive shell.

Extracted from ``server.py`` as part of the planned split
(target ≤400 lines per module).

Requires the host class (``AgentAPIHandler``) to provide:
- ``self._check_auth()`` → bool
- ``self._require_auth(method_name)`` → bool
- ``self._json_response(data, status)``
- ``self._error(status, message)``
- ``self._read_body()`` → dict | None
- ``self._cors()``
- ``self._ws_lock`` — threading.Lock
- ``self.config`` — AppConfig instance
- ``self.store`` — SessionStore instance
- ``self._get_client_ip()`` → str
- ``self._token_hash()`` → str
- ``self.path`` — request path (from BaseHTTPRequestHandler)
- ``self.send_response`` / ``self.send_header`` / ``self.end_headers`` / ``self.wfile``
  (from BaseHTTPRequestHandler)
"""

import json
import logging
import os
import queue
import shlex
import time
from pathlib import Path
from typing import Any

from .safety_guard import SafetyGuard
from .server_shell import shell_open, shell_ensure, shell_close, get_shell_sessions
from .server_sse import sanitize_log, format_sse_data, sse_event_tuple
from .skills import get_skill_manager
from .tools import TOOL_DEFINITIONS

logger = logging.getLogger(__name__)


class ServerRoutesMixin:
    """REST endpoint handlers — health, tools, skills, models, sessions, static files."""

    # ── Handlers ────────────────────────────────────────────────────────

    def _handle_health(self):
        # Public endpoint — only expose status, not internal config
        if self._check_auth():
            skill_mgr = get_skill_manager()
            with self._ws_lock:
                ws = self.config.agent.workspace_dir
            self._json_response({
                "status": "ok",
                "model": self.config.llm.model,
                "workspace": ws,
                "tools": len(TOOL_DEFINITIONS),
                "skills": [s.name for s in skill_mgr.list_skills()],
            })
        else:
            self._json_response({"status": "ok"})

    def _handle_tools(self):
        if not self._require_auth("tools"):
            return
        self._json_response({
            "tools": [
                {"name": t["function"]["name"], "description": t["function"]["description"]}
                for t in TOOL_DEFINITIONS
            ]
        })

    def _handle_skills(self):
        if not self._require_auth("skills"):
            return
        skill_mgr = get_skill_manager()
        self._json_response({
            "skills": [
                {"name": s.name, "description": s.description, "triggers": s.triggers}
                for s in skill_mgr.list_skills()
            ]
        })

    def _handle_models(self):
        """Fetch available models from API if possible, else return cached."""
        try:
            from .model_registry import fetch_available_models
            models = fetch_available_models(self.config.llm.base_url, self.config.llm.api_key)
            if models:
                models_data = [{"id": m, "owned_by": "api"} for m in sorted(models)]
                self._json_response({"models": models_data, "current": self.config.llm.model})
                return
        except Exception:
            logger.debug("Failed to fetch models from API, using cache", exc_info=True)
        # Fallback: cached model list from settings or env
        from .settings import get_settings
        cached = get_settings().get("env", "ATA_CODER_MODELS_CACHE", default="") or self.config.llm.model
        models = [{"id": m.strip(), "owned_by": ""} for m in cached.split(",") if m.strip()]
        self._json_response({"models": models, "current": self.config.llm.model})

    def _handle_set_workspace(self):
        """Change the agent workspace directory."""
        if not self._require_auth("workspace"):
            return
        body = self._read_body()
        if body is None:
            return  # error response already sent by _read_body
        new_ws = body.get("workspace", "")
        if not new_ws:
            self._error(400, "Missing 'workspace' field")
            return
        p = Path(new_ws).expanduser().resolve()
        if not p.exists() or not p.is_dir():
            self._error(400, f"Directory not found or not a directory: {p}")
            return
        ws_str = str(p)
        with self._ws_lock:
            self.config.agent.workspace_dir = ws_str
        # Propagate to all active sessions so existing agents pick up the change.
        # Use the lock-protected list_sessions to avoid concurrent modification.
        for s in self.store.list_sessions():
            sid = s["session_id"]
            agent = self.store.get(sid)
            if agent is not None:
                try:
                    agent.tool_executor.workspace = p
                except Exception:
                    pass
        self._json_response({"workspace": ws_str, "ok": True})

    # ── Sessions ──────────────────────────────────────────────────────────

    def _handle_list_sessions(self):
        if not self._require_auth("sessions"):
            return
        self._json_response({"sessions": self.store.list_sessions()})

    def _handle_get_session(self, sid: str):
        meta = self.store.get_meta(sid)
        if not meta:
            self._error(404, "Session not found")
            return
        info = dict(meta)
        agent = self.store.get(sid)
        if agent:
            info["conversation"] = agent.get_conversation_summary()
        self._json_response(info)

    def _handle_delete_session(self, sid: str):
        if self.store.delete(sid):
            self._json_response({"status": "deleted", "session_id": sid})
        else:
            self._error(404, "Session not found")

    # ── Static file serving ────────────────────────────────────────────────

    def _serve_static(self, rel_path: str):
        """Serve a static file from the web/ directory."""
        web_root = Path(__file__).parent / "web"
        file_path = web_root / rel_path
        if not file_path.exists() or not file_path.is_file():
            self._error(404, f"Not found: {self.path}")
            return
        # Safety: prevent path traversal
        try:
            file_path.resolve().relative_to(web_root.resolve())
        except ValueError:
            self._error(403, "Forbidden")
            return
        content = file_path.read_bytes()
        ct = "text/css" if rel_path.endswith(".css") else "application/javascript"
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", ct + "; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        try:
            self.wfile.write(content)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            pass  # client disconnected

    def _serve_spa(self):
        """Serve the single-page web UI from web/ directory."""
        for fname in ("web/index.html", "web_ui.html"):
            spa_html = Path(__file__).parent / fname
            if spa_html.exists():
                content = spa_html.read_text(encoding="utf-8")
                break
        else:
            content = "<h1>ATA Coder Web UI</h1><p>web/index.html not found.</p>"
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        try:
            self.wfile.write(content.encode("utf-8"))
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            pass  # client disconnected

    # ── Interactive Shell ────────────────────────────────────────────────

    def _handle_shell(self):
        """Interactive PowerShell — persistent session with SSE streaming output."""
        if not self._require_auth("shell"):
            return

        body = self._read_body()
        if body is None:
            return  # error response already sent by _read_body
        sid = body.get("session", "")
        command = body.get("command", "")
        action = body.get("action", "send")

        # Open new session
        if action == "open":
            with self._ws_lock:
                ws = self.config.agent.workspace_dir
            sid = shell_open(ws, token_hash=self._token_hash())
            default_prompt = "PS> " if os.name == "nt" else "$ "
            entry = get_shell_sessions().get(sid, (None, None, None, default_prompt, ""))
            prompt = entry[3] if entry[3] else default_prompt
            self._json_response({"session": sid, "prompt": prompt})
            return

        # Close session
        if action == "close":
            shell_close(sid)
            self._json_response({"ok": True})
            return

        # Send command to persistent session
        if not command:
            self._error(400, "Missing 'command'")
            return
        if not sid:
            self._error(400, "Missing 'session'. Open a session first.")
            return

        # Safety check: run command through the same guard as the agent
        with self._ws_lock:
            ws = self.config.agent.workspace_dir
        guard = SafetyGuard(ws)
        safety = guard.check_shell(command)
        if not safety.allowed:
            logger.warning("⛔ Blocked shell command [%s]: %s", sid[:6], command[:120])
            self._error(403, f"Command blocked: {safety.reason}")
            return
        if safety.warnings:
            for w in safety.warnings:
                logger.warning("⚠️  Shell warning [%s]: %s", sid[:6], w)

        # Additional validation: parse command into argv and check each
        # argument for path traversal or workspace escape attempts.
        try:
            cmd_tokens = shlex.split(command)
        except ValueError:
            cmd_tokens = command.split()
        if cmd_tokens:
            first = cmd_tokens[0]
            # Reject commands with suspicious path characters in the executable name
            if any(c in first for c in ('/', '\\\\', '..')):
                self._error(403, f"Command blocked: suspicious executable path '{first}'")
                return
            # Validate file path arguments stay within workspace
            ws_path = Path(ws).resolve()
            for arg in cmd_tokens[1:]:
                is_pathlike = (
                    arg.startswith('/') or arg.startswith('~') or
                    arg.startswith('..') or '\\\\' in arg or
                    arg.startswith('$')  # env var expansion: $HOME/../../../etc/passwd
                )
                if is_pathlike:
                    try:
                        expanded = os.path.expandvars(os.path.expanduser(arg))
                        resolved = Path(expanded).resolve()
                        resolved.relative_to(ws_path)
                    except (ValueError, OSError):
                        self._error(403, f"Command blocked: argument '{arg}' escapes workspace")
                        return

        logger.info("\U0001f4bb [%s] %s", sid[:6], command[:120])

        proc, outq, lock, prompt, error = shell_ensure(sid, ws, token_hash=self._token_hash())
        if error == "token_mismatch":
            logger.warning("Shell session token mismatch from %s for sid %s", self._get_client_ip(), sid[:6])
            self._error(403, "Session owned by a different client")
            return
        if not proc:
            self._error(500, "Shell process not running")
            return

        # Drain any stale output, send command
        with lock:
            while not outq.empty():
                try: outq.get_nowait()
                except queue.Empty: break
            proc.stdin.write((command + "\n").encode("utf-8"))
            proc.stdin.flush()

        # Stream output via SSE
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        # Adaptive silence timeout: short initial wait, longer between lines
        FAST_TIMEOUT = 0.6     # initial wait for first output (per cycle)
        SLOW_TIMEOUT = 4.0     # silence between output lines
        FIRST_OUTPUT_DEADLINE = time.time() + 10.0  # max wait for any output at all
        deadline = time.time() + 120
        silence_dl = time.time() + FAST_TIMEOUT
        has_output = False
        while time.time() < deadline:
            try:
                line = outq.get(timeout=0.2)
                try:
                    self.wfile.write(
                        f"data: {json.dumps({'text': line})}\n\n".encode("utf-8")
                    )
                    self.wfile.flush()
                except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
                    return  # client disconnected
                has_output = True
                silence_dl = time.time() + SLOW_TIMEOUT
            except queue.Empty:
                pass
            if time.time() > silence_dl:
                if not has_output and time.time() < FIRST_OUTPUT_DEADLINE:
                    silence_dl = time.time() + FAST_TIMEOUT  # keep waiting for first output
                    continue
                break  # silence deadline passed: either got output or gave up on first

        try:
            self.wfile.write(f"event: done\ndata: {json.dumps({'done': True})}\n\n".encode("utf-8"))
            self.wfile.flush()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            pass  # client disconnected

    # ── Chat helpers ─────────────────────────────────────────────────────

    def _parse_chat_request(self) -> dict | None:
        """Read and validate the JSON body for /chat and /chat/stream.

        Returns a dict with keys: message, session_id, skill, model_override,
        thinking_override.  Returns None if the body is invalid (error response
        already sent).
        """
        try:
            body = self._read_body()
        except Exception:
            self._error(400, "Invalid JSON body")
            return None
        if body is None:
            return None  # error response already sent by _read_body
        message = body.get("message", "")
        if not message:
            self._error(400, "Missing 'message' field")
            return None
        return {
            "message": message,
            "session_id": body.get("session_id"),
            "skill": body.get("skill", ""),
            "model_override": body.get("model", ""),
            "thinking_override": body.get("thinking", ""),
        }

    # ── Chat (non-streaming) ────────────────────────────────────────────

    def _handle_chat(self):
        if not self._require_auth("chat"):
            return

        req = self._parse_chat_request()
        if req is None:
            return

        logger.info("\U0001f4e9 [%s] %.200s", time.strftime('%H:%M:%S'), sanitize_log(req["message"]))

        is_new_session = req["session_id"] is None or self.store.get(req["session_id"]) is None
        sid, agent = self.store.get_or_create(req["session_id"], self.config, req["skill"], self._token_hash())

        if req["model_override"]:
            agent.llm.set_model(req["model_override"])
            agent.llm.register_tools(agent._all_tools)
        if req["thinking_override"]:
            agent.llm.config.thinking_strength = req["thinking_override"]

        try:
            import asyncio as _asyncio
            response = _asyncio.run(agent.run(req["message"], stream=False, skill_name=req["skill"] or None,
                                               reset_context=is_new_session))
        except Exception as e:
            logger.exception("Agent run failed: %s", e)
            self._error(500, "Internal server error")
            return

        if response is None:
            response = ""
        if not response and not isinstance(response, str):
            logger.warning("Agent returned non-string response: %r", type(response))
            response = str(response)

        self.store.update_meta(
            sid,
            messages=len(agent._state.messages),
            tool_calls=agent._state.tool_call_count,
        )

        self._json_response({
            "session_id": sid,
            "response": response,
            "tool_calls": agent._state.tool_call_count,
            "tokens": {
                "prompt": agent.llm.total_prompt_tokens,
                "completion": agent.llm.total_completion_tokens,
                "total": agent.llm.total_tokens,
            },
        })

    # ── Chat (SSE streaming) ────────────────────────────────────────────

    def _handle_chat_stream(self):
        if not self._require_auth("chat/stream"):
            return

        req = self._parse_chat_request()
        if req is None:
            return

        logger.info("\U0001f4e9 [%s] %.200s  skill=%s model=%s thinking=%s",
                    time.strftime('%H:%M:%S'), sanitize_log(req["message"]),
                    req["skill"] or "-", req["model_override"] or "-", req["thinking_override"] or "-")

        is_new_session = req["session_id"] is None or self.store.get(req["session_id"]) is None
        sid, agent = self.store.get_or_create(req["session_id"], self.config, req["skill"], self._token_hash())

        if req["model_override"]:
            agent.llm.set_model(req["model_override"])
            agent.llm.register_tools(agent._all_tools)
        if req["thinking_override"]:
            agent.llm.config.thinking_strength = req["thinking_override"]

        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        # ── Streaming loop (async, runs inside asyncio.run()) ──
        import asyncio as _asyncio

        async def _stream_agent():
            events: _asyncio.Queue = _asyncio.Queue()
            done_event = _asyncio.Event()

            def _push_event(evt):
                """Filter events through SSE converter; skip non-streamable ones."""
                tup = sse_event_tuple(evt)
                if tup is not None:
                    events.put_nowait(tup)

            agent.on_event(_push_event)

            result_holder: dict[str, Any] = {"response": "", "error": None}

            async def _run_agent():
                try:
                    logger.info("▶ Agent started")
                    result_holder["response"] = await agent.run(
                        req["message"], stream=True, skill_name=req["skill"] or None,
                        reset_context=is_new_session
                    )
                    logger.info("✓ Agent completed")
                except Exception as exc:
                    logger.info("✗ Agent error: %s", exc)
                    result_holder["error"] = str(exc)
                finally:
                    done_event.set()

            agent_task = _asyncio.create_task(_run_agent())

            # Stream events until agent task completes — use Event for efficient waiting
            while not done_event.is_set():
                get_task = _asyncio.create_task(events.get())
                done_wait = _asyncio.create_task(done_event.wait())
                try:
                    done_, pending = await _asyncio.wait(
                        [get_task, done_wait],
                        return_when=_asyncio.FIRST_COMPLETED,
                    )
                    for t in pending:
                        t.cancel()
                    for t in pending:
                        try:
                            await t
                        except (_asyncio.CancelledError, Exception):
                            pass
                except Exception:
                    get_task.cancel()
                    done_wait.cancel()
                    agent_task.cancel()
                    try:
                        await agent_task
                    except (_asyncio.CancelledError, Exception):
                        pass
                    break

                if get_task in done_ and not get_task.cancelled():
                    try:
                        evt_type, payload = get_task.result()
                    except Exception:
                        continue

                    sse_data = format_sse_data(evt_type, payload)
                    if sse_data is None:
                        continue
                    line = f"event: {evt_type}\ndata: {sse_data}\n\n"
                    try:
                        self.wfile.write(line.encode("utf-8"))
                        self.wfile.flush()
                    except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
                        agent_task.cancel()
                        try:
                            await agent_task
                        except _asyncio.CancelledError:
                            pass
                        return

            # Drain remaining events
            while not events.empty():
                try:
                    evt_type, payload = events.get_nowait()
                    sse_data = format_sse_data(evt_type, payload)
                    if sse_data:
                        line = f"event: {evt_type}\ndata: {sse_data}\n\n"
                        self.wfile.write(line.encode("utf-8"))
                        self.wfile.flush()
                except _asyncio.QueueEmpty:
                    break

            return result_holder

        result_holder = _asyncio.run(_stream_agent())

        # Send final event with session_id so frontend can reuse it
        final = json.dumps({
            "session_id": sid,
            "response": result_holder["response"] or "",
            "error": result_holder["error"],
        })
        try:
            self.wfile.write(f"event: done\ndata: {final}\n\n".encode("utf-8"))
            self.wfile.flush()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            pass

        self.store.update_meta(
            sid,
            messages=len(agent._state.messages),
            tool_calls=agent._state.tool_call_count,
        )
