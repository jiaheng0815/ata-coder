"""
ATA Coder — HTTP API Server (stdlib-only, no FastAPI dependency).

📐 **Planned split** (currently ~1088 lines — target ≤400 per module):
  - ``server_core.py``  — HTTPServer setup, request routing, CORS,
    thread pool management
  - ``server_sse.py``   — SSE streaming, event serialization, client
    disconnect handling
  - ``server_routes.py`` — endpoint handlers (/health, /chat, /sessions, /skills)
  Splitting is deferred to keep the public server API stable.  New
  endpoints should be added to the target sub-module from now on.

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
import secrets
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTPServer with per-request threading — prevents long-running
    chat/tool calls from blocking other clients.

    Limits the thread pool to *max_threads* (default 64) to prevent
    thread exhaustion under DDoS.
    """
    daemon_threads = True  # threads exit when server shuts down
    _max_threads: int = 64

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._thread_lock = threading.Lock()
        self._active_threads = 0

    def process_request(self, request, client_address):
        """Override to enforce a maximum thread count.

        We cannot rely on ThreadingMixIn.process_request() because it spawns a
        daemon thread and returns immediately -- the _active_threads counter
        would be decremented before the handler thread finishes.

        Instead we spawn the thread ourselves and decrement in the thread body.
        """
        with self._thread_lock:
            if self._active_threads >= self._max_threads:
                # Too many active threads — reject with 503
                CRLF = b"\r\n"
                try:
                    body = b'{"error":"Server busy - too many concurrent requests"}'
                    request.sendall(CRLF.join([
                        b"HTTP/1.1 503 Service Unavailable",
                        b"Content-Type: application/json",
                        b"Connection: close",
                        b"Content-Length: " + str(len(body)).encode(),
                        b"",
                        body,
                    ]))
                except Exception:
                    logger.exception("Failed to send 503 response")
                finally:
                    try:
                        request.close()
                    except Exception:
                        logger.exception("Failed to close rejected request socket")
                return
            # Atomic read-modify-write under lock
            self._active_threads += 1

        def _wrap_handler():
            try:
                self.process_request_thread(request, client_address)
            finally:
                with self._thread_lock:
                    self._active_threads = max(0, self._active_threads - 1)

        t = threading.Thread(target=_wrap_handler)
        t.daemon = self.daemon_threads
        t.start()
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Allow running directly (python server.py) without pip install -e .
_pkg = str(Path(__file__).parent.resolve())
if _pkg not in sys.path:
    sys.path.insert(0, _pkg)

from .config import AppConfig, get_config
from .tools import TOOL_DEFINITIONS
from .server_session import SessionStore
from .server_shell import shell_close_all
from .server_routes import ServerRoutesMixin
from .server_rate_limit import RateLimiter

logger = logging.getLogger(__name__)


# Session store → server_session.py (SessionStore)

# ══════════════════════════════════════════════════════════════════════# HTTP Request Handler
# ═══════════════════════════════════════════════════════════════════════════════

class AgentAPIHandler(ServerRoutesMixin, RateLimiter, BaseHTTPRequestHandler):
    """HTTP handler for the ATA Coder API.

    *config* and *store* are set as class attributes by :func:`create_server`
    before the server starts accepting requests.

    Rate limiting is inherited from :class:`RateLimiter`.
    """

    # Class-level references (set by server factory before accepting requests).
    # These are None until create_server() assigns them. mypy note: declared as
    # Optional because they start as None; asserted non-None at usage sites.
    config: "AppConfig | None" = None
    store: "SessionStore | None" = None
    _ws_lock: threading.Lock = threading.Lock()  # protects workspace dir reads/writes

    def __init__(self, *args, **kwargs):
        # Per-instance copies for thread-safe access under ThreadingHTTPServer
        self.config = self.__class__.config
        self.store = self.__class__.store
        super().__init__(*args, **kwargs)

    def log_message(self, format, *args):
        """Suppress default logging; use our logger."""
        logger.debug("%s - %s", self.client_address[0], format % args)

    # ── Auth ────────────────────────────────────────────────────────────

    def _get_client_ip(self) -> str:
        """Return the true client IP, respecting X-Forwarded-For from trusted proxies.

        When the immediate connection comes from localhost (reverse proxy),
        the leftmost IP in X-Forwarded-For is the originating client.
        Otherwise the direct socket address is used — we never trust
        X-Forwarded-For from non-localhost remotes to prevent IP spoofing.
        """
        direct_ip = self.client_address[0]
        # Only trust X-Forwarded-For when the immediate peer is localhost
        if direct_ip not in ("127.0.0.1", "::1", "localhost"):
            return direct_ip
        xff = self.headers.get("X-Forwarded-For", "")
        if not xff:
            return direct_ip
        # X-Forwarded-For: <client>, <proxy1>, <proxy2> — leftmost is origin
        client_ip = xff.split(",")[0].strip()
        # Validate it looks like an IP (v4 or v6)
        import ipaddress
        try:
            ipaddress.ip_address(client_ip)
            logger.debug("Resolved client IP from X-Forwarded-For: %s -> %s", xff, client_ip)
            return client_ip
        except ValueError:
            logger.warning(
                "Malformed X-Forwarded-For from proxy %s: %r — falling back to direct IP",
                direct_ip, xff,
            )
            return direct_ip

    def _check_auth(self) -> bool:
        """Verify API token if ATA_CODER_API_TOKEN is configured.

        When no token is configured, only localhost requests are allowed.
        Set ATA_CODER_API_TOKEN to require Bearer token authentication
        for remote access.
        """
        expected = os.environ.get("ATA_CODER_API_TOKEN", "")
        if not expected:
            # No token configured — require explicit --no-auth flag to allow
            # localhost access without authentication. Otherwise, generate a
            # random token so even localhost requires proof of access.
            if os.environ.get("ATA_CODER_NO_AUTH", "").lower() in ("1", "true", "yes"):
                client_host = self._get_client_ip()
                if client_host in ("127.0.0.1", "::1", "localhost"):
                    return True
                logger.warning(
                    "Remote request from %s rejected: ATA_CODER_NO_AUTH only "
                    "allows localhost. Set ATA_CODER_API_TOKEN for remote access.",
                    client_host,
                )
                return False
            # Secure default: even localhost must present the auto-generated token
            # (printed once at server startup via create_server)
            return False
        token = (self.headers.get("Authorization", "")
                 .removeprefix("Bearer ").strip())
        if not secrets.compare_digest(token, expected):
            logger.warning("Invalid or missing API token from %s", self.client_address[0])
            return False
        return True

    def _token_hash(self) -> str:
        """Return a hash of the current request's auth token for session isolation."""
        import hashlib
        token = (self.headers.get("Authorization", "")
                 .removeprefix("Bearer ").strip())
        return hashlib.sha256(token.encode()).hexdigest()[:16] if token else ""

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
        """Set CORS headers, reflecting localhost origins.

        Allows any localhost origin (multiple ports) for development.
        Non-localhost origins are NOT reflected — they should use token auth.
        """
        origin = self.headers.get("Origin", "")
        if origin and (
            origin.startswith("http://localhost")
            or origin.startswith("http://127.0.0.1")
            or origin.startswith("http://[::1]")
        ):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        elif os.environ.get("ATA_CODER_API_TOKEN"):
            # Token auth configured — only allow the configured origin or localhost
            self.send_header("Access-Control-Allow-Origin", "http://localhost:3000")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        # else: no CORS headers — non-localhost requests without auth are rejected

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
        try:
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            pass  # client disconnected before response could be sent

    def _error(self, status: int, message: str):
        self._json_response({"error": message}, status)

    def _read_body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            length = 0
        if length <= 0:  # reject 0, negative, and non-numeric Content-Length
            return {}
        # Validate Content-Type
        ct = self.headers.get("Content-Type", "")
        if "application/json" not in ct:
            self._error(415, "Content-Type must be application/json")
            return None
        if length > 10_000_000:  # 10MB max
            self._error(413, f"Request body too large ({length:,} bytes). Max 10MB.")
            return None
        body = self.rfile.read(length)
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            self._error(400, f"Invalid JSON: {e}")
            return None

    def _path_parts(self) -> list[str]:
        parsed = urlparse(self.path)
        return [p for p in parsed.path.split("/") if p]

    # ── Routing ─────────────────────────────────────────────────────────

    def do_GET(self):
        if self.path != "/favicon.ico":
            logger.debug("GET %s", self.path)
        parts = self._path_parts()

        # ── Rate limiting ────────────────────────────────────────────────
        if not self._check_rate_limit(self.client_address[0]):
            self._error(429, "Too many requests. Please slow down.")
            return

        if self.path == "/" or self.path == "/index.html":
            self._serve_spa()
        elif self.path == "/health":
            self._handle_health()
        elif self.path == "/tools":
            if not self._require_auth("tools"): return
            self._handle_tools()
        elif self.path == "/skills":
            if not self._require_auth("skills"): return
            self._handle_skills()
        elif self.path == "/models":
            if not self._require_auth("models"): return
            self._handle_models()
        elif self.path == "/sessions":
            if not self._require_auth("sessions"): return
            self._handle_list_sessions()
        elif self.path == "/api/workspace":
            if not self._require_auth("workspace"): return
            with self._ws_lock:
                ws = self.config.agent.workspace_dir
            self._json_response({"workspace": ws})
        elif self.path.startswith("/css/") or self.path.startswith("/js/"):
            self._serve_static(self.path.lstrip("/"))
        elif len(parts) == 2 and parts[0] == "sessions":
            if not self._require_auth("sessions"): return
            self._handle_get_session(parts[1])
        else:
            self._error(404, f"Not found: {self.path}")

    def do_POST(self):
        logger.debug("POST %s", self.path)
        if not self._check_rate_limit(self.client_address[0]):
            self._error(429, "Rate limit exceeded. Try again shortly.")
            return
        if self.path == "/chat":
            self._handle_chat()
        elif self.path == "/chat/stream":
            self._handle_chat_stream()
        elif self.path == "/api/workspace":
            self._handle_set_workspace()
        elif self.path == "/api/shell":
            self._handle_shell()
        else:
            self._error(404, f"Not found: {self.path}")

    def do_DELETE(self):
        if not self._check_rate_limit(self.client_address[0]):
            self._error(429, "Rate limit exceeded. Try again shortly.")
            return
        if not self._require_auth("DELETE"):
            return
        parts = self._path_parts()
        if len(parts) == 2 and parts[0] == "sessions":
            self._handle_delete_session(parts[1])
        else:
            self._error(404, f"Not found: {self.path}")

    # _parse_chat_request / _handle_chat → ServerRoutesMixin (server_routes.py)

    # ── Handlers ────────────────────────────────────────────────────────
    # _handle_health / _handle_tools / _handle_skills / _handle_models /
    # _handle_set_workspace / _handle_shell → ServerRoutesMixin (server_routes.py)

    # _serve_static / _serve_spa → ServerRoutesMixin (server_routes.py)

    # _handle_list_sessions / _handle_get_session / _handle_delete_session
    # → ServerRoutesMixin (server_routes.py)

    # _handle_chat_stream → ServerRoutesMixin (server_routes.py)

# ═══════════════════════════════════════════════════════════════════════════════
# Server factory
# ═══════════════════════════════════════════════════════════════════════════════

# Shell management → server_shell.py

def create_server(
    config: AppConfig | None = None,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> HTTPServer:
    """Create and configure the HTTP API server."""

    # Common mistakes: URLs, port numbers
    if host.startswith("http://") or host.startswith("https://"):
        host = host.split("://", 1)[1].rstrip("/")
    if host.isdigit():
        port = int(host)
        host = "127.0.0.1"

    config = config or get_config()

    # Auto-generate API token if none configured (secure default)
    if not os.environ.get("ATA_CODER_API_TOKEN") and os.environ.get("ATA_CODER_NO_AUTH", "").lower() not in ("1", "true", "yes"):
        auto_token = secrets.token_urlsafe(24)
        os.environ["ATA_CODER_API_TOKEN"] = auto_token
        logger.info(
            "🔐 No ATA_CODER_API_TOKEN set — auto-generated: %s\n"
            "   Pass this token as 'Authorization: Bearer %s' header.\n"
            "   Set ATA_CODER_NO_AUTH=1 to disable auth for localhost-only use.",
            auto_token, auto_token,
        )

    AgentAPIHandler.config = config
    AgentAPIHandler.store = SessionStore()

    try:
        server = ThreadingHTTPServer((host, port), AgentAPIHandler)
    except OSError as e:
        if "10048" in str(e) or "98" in str(e) or "Address already in use" in str(e):
            logger.error("Port %d is already in use. Use --port to pick another, "
                        "or check: netstat -ano | findstr :%d", port, port)
            raise SystemExit(1) from e
        raise

    # Close idle connections after 30s to prevent file descriptor exhaustion
    server.socket.settimeout(30.0)
    server.timeout = 30

    logger.info("Server created: %s:%d", host, port)
    return server


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
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
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (127.0.0.1 = local only, 0.0.0.0 = LAN accessible)")
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

    # Security: warn if binding to all interfaces without authentication
    # (checked AFTER create_server() because it may auto-generate a token)
    if args.host == "0.0.0.0" and not os.environ.get("ATA_CODER_API_TOKEN"):
        logger.warning(
            "⚠️  Binding to 0.0.0.0 WITHOUT an API token — "
            "anyone on the network can access the agent. "
            "Set ATA_CODER_API_TOKEN env var or use --local-only."
        )

    # Detect LAN IP for mobile access
    lan_ip = _detect_lan_ip() if args.host == "0.0.0.0" else None

    print("""
╔══════════════════════════════════════════════════╗
║         ATA Coder  —  Web UI              ║
╠══════════════════════════════════════════════════╣""")
    print(f"║  Local:   http://127.0.0.1:{args.port:<29}║")
    if lan_ip:
        print(f"║  LAN:     http://{lan_ip}:{args.port:<29}║")
    else:
        print("║  LAN:     (use --host 0.0.0.0 for LAN access) ║")
    print(f"""║  Model:   {config.llm.model:<34}║
║  Tools:   {len(TOOL_DEFINITIONS):<34}║
╚══════════════════════════════════════════════════╝
""")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        shell_close_all()
        server.shutdown()


if __name__ == "__main__":
    main()
