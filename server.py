"""
ATA Coder — HTTP API Server (stdlib-only, no FastAPI dependency).

Uses Python's built-in http.server + httpx for maximum compatibility.
Provides REST API and SSE streaming for the agent.

Endpoints:
  POST /chat              — Non-streaming chat
  POST /chat/stream       — SSE streaming chat
  GET  /health            — Health check
  GET  /sessions          — List active sessions
  GET  /sessions/{id}     — Get session info
  DELETE /sessions/{id}   — Delete a session
  GET  /tools             — List available tools
  GET  /skills            — List available skills
  GET  /models            — List available models

Usage:
  python server.py                        # Start on port 8000
  python server.py --port 3000            # Custom port
  python server.py --host 0.0.0.0         # Public access
  python main.py --server                 # From main launcher
"""

import json
import logging
import os
import sys
import threading
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).parent.resolve()))

from .config import AppConfig, get_config
from .agent import CoderAgent
from .tools import ToolExecutor, TOOL_DEFINITIONS
from .agent_subsystems import AgentSubsystems
from .permissions import PermissionStore, PermissionMode

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Session store (thread-safe in-memory)
# ═══════════════════════════════════════════════════════════════════════════════

class SessionStore:
    """Thread-safe session store for the API server."""

    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: dict[str, CoderAgent] = {}
        self._metadata: dict[str, dict] = {}

    def create(self, config: AppConfig, skill: str = "") -> tuple[str, CoderAgent]:
        sid = str(uuid.uuid4())[:12]

        tool_exec = ToolExecutor(config.agent)

        # Build AgentSubsystems container (replaces loose kwargs)
        perms = PermissionStore(config.agent.workspace_dir)

        # API mode: default to allow (caller implements own auth)
        if os.environ.get("ATA_CODER_ALLOW_ALL", "").lower() in ("1", "true", "yes"):
            perms.set_category_rule("shell", PermissionMode.ALLOW)
            perms.set_category_rule("write", PermissionMode.ALLOW)
        else:
            perms.set_prompt_callback(lambda n, a, c: True)

        from .skills import get_skill_manager
        from .memory import get_memory_store
        from .project import ProjectDetector

        skill_mgr = get_skill_manager()
        if skill:
            skill_mgr.activate(skill)

        subsystems = AgentSubsystems(
            skills=skill_mgr,
            memory=get_memory_store(),
            permissions=perms,
            project_info=ProjectDetector(config.agent.workspace_dir).detect(),
        )

        agent = CoderAgent(
            config=config,
            tool_executor=tool_exec,
            subsystems=subsystems,
        )

        with self._lock:
            self._sessions[sid] = agent
            self._metadata[sid] = {
                "created": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "messages": 0,
                "tool_calls": 0,
                "skill": skill,
                "model": config.llm.model,
            }
        return sid, agent

    def get(self, sid: str) -> CoderAgent | None:
        with self._lock:
            return self._sessions.get(sid)

    def get_or_create(self, sid: str | None, config: AppConfig, skill: str = "") -> tuple[str, CoderAgent]:
        if sid:
            existing = self.get(sid)
            if existing:
                return sid, existing
        return self.create(config, skill)

    def update_meta(self, sid: str, **kwargs):
        with self._lock:
            if sid in self._metadata:
                self._metadata[sid].update(kwargs)

    def list_sessions(self) -> list[dict]:
        with self._lock:
            return [
                {"session_id": sid, **meta}
                for sid, meta in self._metadata.items()
            ]

    def get_meta(self, sid: str) -> dict | None:
        with self._lock:
            return self._metadata.get(sid)

    def delete(self, sid: str) -> bool:
        with self._lock:
            agent = self._sessions.pop(sid, None)
            if agent:
                try:
                    agent.shutdown()
                except Exception:
                    pass
            self._metadata.pop(sid, None)
            return agent is not None


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP Request Handler
# ═══════════════════════════════════════════════════════════════════════════════

class AgentAPIHandler(BaseHTTPRequestHandler):
    """HTTP handler for the ATA Coder API."""

    # Class-level references (set by server factory)
    config: AppConfig = None
    store: SessionStore = None

    def log_message(self, format, *args):
        """Suppress default logging; use our logger."""
        logger.debug("%s - %s", self.client_address[0], format % args)

    # ── Auth ────────────────────────────────────────────────────────────

    def _check_auth(self) -> bool:
        """Verify API token if ATA_CODER_API_TOKEN is configured."""
        expected = os.environ.get("ATA_CODER_API_TOKEN", "")
        if not expected:
            return True  # no token configured → allow all (backward compat)
        token = (self.headers.get("Authorization", "")
                 .removeprefix("Bearer ").strip())
        return token == expected

    def _require_auth(self, method_name: str) -> bool:
        """Check auth and send 403 if invalid. Returns True if ok."""
        if self._check_auth():
            return True
        self._error(403, "Invalid or missing API token. "
                   "Set ATA_CODER_API_TOKEN env var on the server, "
                   "then send Authorization: Bearer <token> header.")
        return False

    # ── CORS ────────────────────────────────────────────────────────────

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    # ── JSON helpers ────────────────────────────────────────────────────

    def _json_response(self, data: Any, status: int = 200):
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _error(self, status: int, message: str):
        self._json_response({"error": message}, status)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        body = self.rfile.read(length)
        return json.loads(body)

    def _path_parts(self) -> list[str]:
        parsed = urlparse(self.path)
        return [p for p in parsed.path.split("/") if p]

    # ── Routing ─────────────────────────────────────────────────────────

    def do_GET(self):
        parts = self._path_parts()

        if self.path == "/" or self.path == "/index.html":
            self._serve_spa()
        elif self.path == "/health":
            self._handle_health()
        elif self.path == "/tools":
            self._handle_tools()
        elif self.path == "/skills":
            self._handle_skills()
        elif self.path == "/models":
            self._handle_models()
        elif self.path == "/sessions":
            self._handle_list_sessions()
        elif self.path == "/api/workspace":
            self._json_response({"workspace": self.config.agent.workspace_dir})
        elif len(parts) == 2 and parts[0] == "sessions":
            self._handle_get_session(parts[1])
        else:
            self._error(404, f"Not found: {self.path}")

    def do_POST(self):
        # Auth is optional for local/LAN use (web UI is served from same origin)
        if self.path == "/chat":
            self._handle_chat()
        elif self.path == "/chat/stream":
            self._handle_chat_stream()
        elif self.path == "/api/workspace":
            self._handle_set_workspace()
        else:
            self._error(404, f"Not found: {self.path}")

    def do_DELETE(self):
        if not self._require_auth("DELETE"):
            return
        parts = self._path_parts()
        if len(parts) == 2 and parts[0] == "sessions":
            self._handle_delete_session(parts[1])
        else:
            self._error(404, f"Not found: {self.path}")

    # ── Handlers ────────────────────────────────────────────────────────

    def _handle_health(self):
        skill_mgr = get_skill_manager()
        self._json_response({
            "status": "ok",
            "model": self.config.llm.model,
            "workspace": self.config.agent.workspace_dir,
            "tools": len(TOOL_DEFINITIONS),
            "skills": [s.name for s in skill_mgr.list_skills()],
            "mcp_servers": [],
        })

    def _handle_tools(self):
        self._json_response({
            "tools": [
                {"name": t["function"]["name"], "description": t["function"]["description"]}
                for t in TOOL_DEFINITIONS
            ]
        })

    def _handle_skills(self):
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
            pass
        # Fallback: cached model list
        import os
        cached = os.environ.get("ATA_CODER_MODELS_CACHE", self.config.llm.model)
        models = [{"id": m.strip(), "owned_by": ""} for m in cached.split(",") if m.strip()]
        self._json_response({"models": models, "current": self.config.llm.model})

    def _handle_set_workspace(self):
        """Change the agent workspace directory."""
        body = self._read_body()
        new_ws = body.get("workspace", "")
        if not new_ws:
            self._error(400, "Missing 'workspace' field")
            return
        p = Path(new_ws).expanduser().resolve()
        if not p.exists():
            self._error(400, f"Directory not found: {p}")
            return
        self.config.agent.workspace_dir = str(p)
        self._json_response({"workspace": str(p), "ok": True})

    def _serve_spa(self):
        """Serve the single-page web UI."""
        spa_html = Path(__file__).parent / "web_ui.html"
        if spa_html.exists():
            content = spa_html.read_text(encoding="utf-8")
        else:
            content = "<h1>ATA Coder Web UI</h1><p>web_ui.html not found.</p>"
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(content.encode("utf-8"))

    def _handle_list_sessions(self):
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

    # ── Chat (non-streaming) ────────────────────────────────────────────

    def _handle_chat(self):
        try:
            body = self._read_body()
        except Exception:
            self._error(400, "Invalid JSON body")
            return

        message = body.get("message", "")
        if not message:
            self._error(400, "Missing 'message' field")
            return

        # ── Log incoming request ──────────────────────────────────────────
        print(f"\n{'═'*60}")
        print(f"📩 [{time.strftime('%H:%M:%S')}] {message[:200]}")
        print(f"{'═'*60}\n")

        session_id = body.get("session_id")
        skill = body.get("skill", "")
        model_override = body.get("model", "")
        thinking_override = body.get("thinking", "")

        sid, agent = self.store.get_or_create(session_id, self.config, skill)

        if model_override:
            agent.llm.set_model(model_override)
            agent.llm.register_tools(agent._all_tools)
        if thinking_override:
            agent.llm.config.thinking_strength = thinking_override

        try:
            response = agent.run(message, stream=False, skill_name=skill or None)
        except Exception as e:
            logger.exception("Agent run failed")
            self._error(500, str(e))
            return

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
        try:
            body = self._read_body()
        except Exception:
            self._error(400, "Invalid JSON body")
            return

        message = body.get("message", "")
        if not message:
            self._error(400, "Missing 'message' field")
            return

        session_id = body.get("session_id")
        skill = body.get("skill", "")
        model_override = body.get("model", "")
        thinking_override = body.get("thinking", "")

        # ── Log incoming request ──────────────────────────────────────────
        print(f"\n{'═'*60}")
        print(f"📩 [{time.strftime('%H:%M:%S')}] {message[:200]}")
        if skill:   print(f"   skill={skill}")
        if model_override: print(f"   model={model_override}")
        if thinking_override: print(f"   thinking={thinking_override}")
        print(f"{'─'*60}")

        sid, agent = self.store.get_or_create(session_id, self.config, skill)

        if model_override:
            agent.llm.set_model(model_override)
            agent.llm.register_tools(agent._all_tools)
        if thinking_override:
            agent.llm.config.thinking_strength = thinking_override

        # Send SSE headers
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        events = []
        events_lock = threading.Lock()

        def on_event(event):
            from .agent import (TextDeltaEvent, ToolCallEvent, ToolResultEvent,
                                CompleteEvent, ErrorEvent, ReasoningEvent, ThinkingEvent)
            import json as _json

            if isinstance(event, TextDeltaEvent):
                ev = {"type": "text", "text": event.text}
                print(_json.dumps(ev, ensure_ascii=False))
                with events_lock:
                    events.append(("text", event.text))
            elif isinstance(event, (ReasoningEvent, ThinkingEvent)):
                ev = {"type": "thinking", "text": event.text[:300]}
                print(_json.dumps(ev, ensure_ascii=False))
                with events_lock:
                    events.append(("thinking", event.text[:200]))
            elif isinstance(event, ToolCallEvent):
                ev = {"type": "tool_call", "tool": event.tool_name, "source": event.source, "args": _brief_dict(event.arguments)}
                print(_json.dumps(ev, ensure_ascii=False))
                with events_lock:
                    events.append(("tool_call", {"name": event.tool_name, "arguments": event.arguments, "source": event.source}))
            elif isinstance(event, ToolResultEvent):
                ev = {"type": "tool_result", "tool": event.tool_name, "ok": event.result.success, "output": (event.result.output or "")[:300]}
                print(_json.dumps(ev, ensure_ascii=False))
                with events_lock:
                    events.append(("tool_result", {"name": event.tool_name, "success": event.result.success, "output": (event.result.output or "")[:500], "error": event.result.error}))
            elif isinstance(event, ErrorEvent):
                ev = {"type": "error", "error": event.error}
                print(_json.dumps(ev, ensure_ascii=False))
                with events_lock:
                    events.append(("error", {"error": event.error}))
            elif isinstance(event, CompleteEvent):
                ev = {"type": "complete", "tools": event.total_tool_calls, "time": round(event.total_time, 1)}
                print(_json.dumps(ev, ensure_ascii=False))
                print(f"{'═'*60}\n")
                with events_lock:
                    events.append(("complete", {"tool_calls": event.total_tool_calls, "time": event.total_time}))

        agent.on_event(on_event)

        result_holder = {"response": "", "error": None}

        def run_agent():
            try:
                result_holder["response"] = agent.run(message, stream=True, skill_name=skill or None)
            except Exception as e:
                result_holder["error"] = str(e)

        thread = threading.Thread(target=run_agent)
        thread.start()

        # Stream events
        last_idx = 0
        while thread.is_alive() or last_idx < len(events):
            with events_lock:
                while last_idx < len(events):
                    evt_type, data = events[last_idx]
                    last_idx += 1
                    if isinstance(data, str):
                        # Text delta — send as SSE data only
                        line = f"event: {evt_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                    else:
                        line = f"event: {evt_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                    try:
                        self.wfile.write(line.encode("utf-8"))
                        self.wfile.flush()
                    except Exception:
                        return  # client disconnected

            time.sleep(0.05)

        # Send final event
        final = json.dumps({
            "session_id": sid,
            "response": result_holder["response"] or "",
            "error": result_holder["error"],
        })
        try:
            self.wfile.write(f"event: done\ndata: {final}\n\n".encode("utf-8"))
            self.wfile.flush()
        except Exception:
            pass

        self.store.update_meta(
            sid,
            messages=len(agent._state.messages),
            tool_calls=agent._state.tool_call_count,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Server factory
# ═══════════════════════════════════════════════════════════════════════════════

def create_server(
    config: AppConfig | None = None,
    host: str = "0.0.0.0",
    port: int = 8000,
) -> HTTPServer:
    """Create and configure the HTTP API server."""

    # Common mistakes: URLs, port numbers
    if host.startswith("http://") or host.startswith("https://"):
        host = host.split("://", 1)[1].rstrip("/")
    if host.isdigit():
        port = int(host)
        host = "0.0.0.0"

    config = config or get_config()

    AgentAPIHandler.config = config
    AgentAPIHandler.store = SessionStore()

    server = HTTPServer((host, port), AgentAPIHandler)

    logger.info("Server created: %s:%d", host, port)
    return server


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
def _brief_dict(d: dict) -> dict:
    """Truncate string values for readable console output."""
    if not d:
        return {}
    out = {}
    for k, v in d.items():
        if isinstance(v, str) and len(v) > 100:
            out[k] = v[:100] + "..."
        else:
            out[k] = v
    return out


def _detect_lan_ip() -> str | None:
    """Detect the LAN IP address for mobile/tablet access."""
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        pass
    # Fallback: iterate network interfaces
    try:
        import socket
        hostname = socket.gethostname()
        ip = socket.gethostbyname(hostname)
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="ATA Coder API Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (0.0.0.0 = LAN accessible)")
    parser.add_argument("--port", "-p", type=int, default=8000, help="Bind port")
    parser.add_argument("--local-only", action="store_true", help="Bind to 127.0.0.1 only (no LAN)")
    parser.add_argument("--allow-all", "-A", action="store_true", help="Skip all permission prompts")
    parser.add_argument("--model", "-m", help="Model name")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    # Server mode: verbose by default so you can see what the agent is doing
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.allow_all:
        os.environ["ATA_CODER_ALLOW_ALL"] = "1"

    config = get_config()
    if args.model:
        config.llm.model = args.model

    if args.local_only:
        args.host = "127.0.0.1"

    server = create_server(config, args.host, args.port)

    # Detect LAN IP for mobile access
    lan_ip = _detect_lan_ip() if args.host == "0.0.0.0" else None

    print(f"""
╔══════════════════════════════════════════════════╗
║         ATA Coder  —  Web UI              ║
╠══════════════════════════════════════════════════╣""")
    print(f"║  Local:   http://127.0.0.1:{args.port:<29}║")
    if lan_ip:
        print(f"║  LAN:     http://{lan_ip}:{args.port:<29}║")
    else:
        print(f"║  LAN:     (use --host 0.0.0.0 for LAN access) ║")
    print(f"""║  Model:   {config.llm.model:<34}║
║  Tools:   {len(TOOL_DEFINITIONS):<34}║
╚══════════════════════════════════════════════════╝
""")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
