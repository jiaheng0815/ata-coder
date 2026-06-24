"""
MCP (Model Context Protocol) client — full-spec implementation.

Supports MCP servers over:
- stdio (subprocess): spawns the server as a child process
- HTTP/SSE: connects to a remote MCP server

Implements: capability negotiation, tools, resources, prompts, ping,
progress notifications, cancellation, resource templates, logging,
completion, roots.

Spec: https://spec.modelcontextprotocol.io/
"""

import asyncio
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# JSON-RPC 2.0 standard error codes
# ═══════════════════════════════════════════════════════════════════════════════

class JsonRpcError(Exception):
    """A JSON-RPC error with standard code and message."""
    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(message)

# Standard JSON-RPC error codes
PARSE_ERROR      = -32700
INVALID_REQUEST  = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS   = -32602
INTERNAL_ERROR   = -32603
# MCP-specific (server error range: -32000 to -32099)
SERVER_NOT_INITIALIZED = -32002
REQUEST_CANCELLED      = -32800


# ═══════════════════════════════════════════════════════════════════════════════
# JSON-RPC types
# ═══════════════════════════════════════════════════════════════════════════════

JsonRpcId = str | int


@dataclass
class JsonRpcRequest:
    jsonrpc: str = "2.0"
    method: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    id: JsonRpcId = ""


@dataclass
class JsonRpcResponse:
    jsonrpc: str = "2.0"
    result: Any = None
    error: dict[str, Any] | None = None
    id: JsonRpcId = ""


# ═══════════════════════════════════════════════════════════════════════════════
# MCP Server connection — base class
# ═══════════════════════════════════════════════════════════════════════════════

class MCPServerConnection:
    """
    A connection to a single MCP server.

    Handles JSON-RPC communication, capability negotiation,
    tool/resource/prompt discovery, ping, progress, and cancellation.
    """

    PROTOCOL_VERSION = "2025-03-26"

    def __init__(self, name: str):
        self.name = name
        self._tools: list[dict[str, Any]] = []
        self._resources: list[dict[str, Any]] = []
        self._resource_templates: list[dict[str, Any]] = []
        self._prompts: list[dict[str, Any]] = []
        self._initialized: bool = False
        self._server_info: dict[str, Any] = {}
        self._capabilities: dict[str, Any] = {}
        self._server_capabilities: dict[str, Any] = {}
        self._roots: list[dict[str, Any]] = []
        self._pong_received: bool = False

    @property
    def tools(self) -> list[dict[str, Any]]:
        return self._tools

    @property
    def resources(self) -> list[dict[str, Any]]:
        return self._resources

    @property
    def resource_templates(self) -> list[dict[str, Any]]:
        return self._resource_templates

    @property
    def prompts(self) -> list[dict[str, Any]]:
        return self._prompts

    @property
    def initialized(self) -> bool:
        return self._initialized

    @property
    def server_info(self) -> dict[str, Any]:
        return self._server_info

    @property
    def capabilities(self) -> dict[str, Any]:
        return self._capabilities

    @property
    def server_capabilities(self) -> dict[str, Any]:
        return self._server_capabilities

    # ── Abstract methods ──

    async def start(self) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError

    async def send_request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        raise NotImplementedError

    async def send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        raise NotImplementedError

    # ── Capability checks ──

    def has_capability(self, cap: str) -> bool:
        """Check if the server supports a given capability namespace."""
        return cap in self._server_capabilities

    def has_subcapability(self, cap: str, sub: str) -> bool:
        """Check if the server supports a sub-capability (e.g. tools→listChanged)."""
        caps = self._server_capabilities.get(cap, {})
        return isinstance(caps, dict) and sub in caps

    # ── Lifecycle ──

    async def initialize(self, client_capabilities: dict[str, Any] | None = None) -> None:
        """Send initialize request and negotiate capabilities."""
        caps = {
            "tools": {},
            "resources": {"subscribe": True},
            "prompts": {},
            "logging": {},
        }
        if client_capabilities:
            caps.update(client_capabilities)

        init_result = await self.send_request("initialize", {
            "protocolVersion": self.PROTOCOL_VERSION,
            "capabilities": caps,
            "clientInfo": {
                "name": "ata-coder",
                "version": "2.3.0",
            },
        })
        self._server_info = init_result.get("serverInfo", {})
        self._server_capabilities = init_result.get("capabilities", {})
        self._initialized = True

        # Send initialized notification
        await self.send_notification("notifications/initialized", {})

        logger.info(
            "[%s] Initialized: %s v%s  (caps: %s)",
            self.name,
            self._server_info.get("name", "unknown"),
            self._server_info.get("version", "?"),
            ", ".join(self._server_capabilities) or "none",
        )

    async def discover(self) -> None:
        """Discover tools, resources, resource templates, and prompts."""
        if not self._initialized:
            raise JsonRpcError(SERVER_NOT_INITIALIZED, "Server not initialized")

        # Tools
        if self.has_capability("tools"):
            try:
                result = await self.send_request("tools/list", {})
                self._tools = result.get("tools", [])
                logger.info("[%s] Discovered %d tools", self.name, len(self._tools))
            except Exception:
                logger.warning("[%s] Failed to discover tools", self.name, exc_info=True)

        # Resources
        if self.has_capability("resources"):
            try:
                result = await self.send_request("resources/list", {})
                self._resources = result.get("resources", [])
                logger.info("[%s] Discovered %d resources", self.name, len(self._resources))
            except Exception:
                logger.warning("[%s] Failed to discover resources", self.name, exc_info=True)

        # Resource templates
        if self.has_capability("resources"):
            try:
                result = await self.send_request("resources/templates/list", {})
                self._resource_templates = result.get("resourceTemplates", [])
                logger.info("[%s] Discovered %d resource templates", self.name, len(self._resource_templates))
            except Exception:
                logger.warning("[%s] Failed to discover resource templates", self.name, exc_info=True)

        # Prompts
        if self.has_capability("prompts"):
            try:
                result = await self.send_request("prompts/list", {})
                self._prompts = result.get("prompts", [])
                logger.info("[%s] Discovered %d prompts", self.name, len(self._prompts))
            except Exception:
                logger.warning("[%s] Failed to discover prompts", self.name, exc_info=True)

    # ── Tool calling ──

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Call a tool on this MCP server."""
        if not self.has_capability("tools"):
            raise JsonRpcError(METHOD_NOT_FOUND, "Server does not support tools")
        return await self.send_request("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })

    # ── Resource reading ──

    async def read_resource(self, uri: str) -> Any:
        """Read a resource by URI."""
        if not self.has_capability("resources"):
            raise JsonRpcError(METHOD_NOT_FOUND, "Server does not support resources")
        return await self.send_request("resources/read", {"uri": uri})

    async def subscribe_resource(self, uri: str) -> None:
        """Subscribe to resource updates."""
        if not self.has_subcapability("resources", "subscribe"):
            return
        await self.send_notification("resources/subscribe", {"uri": uri})
        logger.info("[%s] Subscribed to: %s", self.name, uri)

    # ── Prompts ──

    async def get_prompt(self, name: str, arguments: dict[str, str] | None = None) -> Any:
        """Get a prompt by name with optional arguments."""
        if not self.has_capability("prompts"):
            raise JsonRpcError(METHOD_NOT_FOUND, "Server does not support prompts")
        params: dict[str, Any] = {"name": name}
        if arguments:
            params["arguments"] = arguments
        return await self.send_request("prompts/get", params)

    # ── Ping ──

    async def ping(self, timeout: float = 10.0) -> bool:
        """Ping the server. Returns True if alive."""
        try:
            await asyncio.wait_for(self.send_request("ping", {}), timeout=timeout)
            return True
        except Exception:
            return False

    # ── Completion ──

    async def complete(self, ref: dict[str, Any], argument: dict[str, Any]) -> Any:
        """Request auto-completion for a prompt or resource template argument."""
        return await self.send_request("completion/complete", {
            "ref": ref,
            "argument": argument,
        })

    # ── Roots ──

    async def set_roots(self, roots: list[dict[str, Any]]) -> None:
        """Inform the server about root directories."""
        self._roots = roots
        await self.send_notification("notifications/roots/list_changed", {"roots": roots})
        logger.info("[%s] Updated roots: %d", self.name, len(roots))

    # ── Logging ──

    async def set_log_level(self, level: str) -> None:
        """Set the log level on the server (debug/info/notice/warning/error/critical)."""
        await self.send_notification("logging/setLevel", {"level": level})


# ═══════════════════════════════════════════════════════════════════════════════
# Stdio connection
# ═══════════════════════════════════════════════════════════════════════════════

class StdioMCPConnection(MCPServerConnection):
    """MCP connection over stdio (subprocess)."""

    def __init__(self, name: str, command: str, args: list[str] | None = None,
                 env: dict[str, str] | None = None, cwd: str | None = None):
        super().__init__(name)
        self._next_req_id = 0  # per-instance counter (was shared class var)
        self.command = command
        self.args = args or []
        self.env = env
        self.cwd = cwd
        self._process: asyncio.subprocess.Process | None = None
        self._pending: dict[JsonRpcId, asyncio.Event] = {}
        self._results: dict[JsonRpcId, JsonRpcResponse] = {}
        self._reader_task: asyncio.Task | None = None
        self._running = False
        self._on_progress: Callable[[int, int, str | None], None] | None = None

    def _next_id(self) -> str:
        self._next_req_id += 1
        return str(self._next_req_id)

    def on_progress(self, callback: Callable[[int, int, str | None], None]) -> None:
        """Register a callback for progress notifications."""
        self._on_progress = callback

    # ── Start / Stop ──

    async def start(self) -> None:
        """Start the MCP server process."""
        logger.info("[%s] Starting: %s %s", self.name, self.command, " ".join(self.args))

        try:
            self._process = await asyncio.create_subprocess_exec(
                self.command, *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=self.env,
                cwd=self.cwd,
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"MCP server command not found: {self.command}. "
                f"Install it or check the path."
            )
        except Exception as e:
            raise RuntimeError(f"Failed to start MCP server: {e}") from e

        self._running = True
        self._reader_task = asyncio.create_task(self._read_loop())

        try:
            await self.initialize()
            await self.discover()
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        """Stop the MCP server process."""
        self._running = False

        # Cancel and await reader task FIRST — it holds the stdout pipe open.
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None

        # Terminate/kill the process
        proc = self._process
        self._process = None
        if proc is not None:
            try:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                        await asyncio.wait_for(proc.wait(), timeout=3)
                    except Exception:
                        pass
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            # Explicitly close pipes to prevent "I/O operation on closed pipe"
            # during BaseSubprocessTransport.__del__ at GC time.
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except Exception:
                        pass

        # Release all pending requests
        for evt in self._pending.values():
            evt.set()
        self._pending.clear()
        self._results.clear()

        logger.info("[%s] Stopped", self.name)

    # ── Message I/O ──

    async def _send_raw(self, msg: dict[str, Any]) -> None:
        """Send a raw JSON-RPC message to the server."""
        if not self._process or not self._process.stdin:
            raise RuntimeError("MCP server not running")
        line = json.dumps(msg, ensure_ascii=False) + "\n"
        try:
            self._process.stdin.write(line.encode("utf-8"))
            await self._process.stdin.drain()
        except Exception as e:
            raise RuntimeError(f"Failed to send to MCP server: {e}") from e

    async def send_request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send a JSON-RPC request and wait for the response."""
        req_id = self._next_id()
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": req_id,
        }

        event = asyncio.Event()
        self._pending[req_id] = event

        await self._send_raw(msg)

        # Wait with timeout
        timeout = 120 if method == "initialize" else 60
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise JsonRpcError(INTERNAL_ERROR, f"MCP request timeout: {method}")

        response = self._results.pop(req_id, None)

        if response is None:
            raise JsonRpcError(INTERNAL_ERROR, f"No response for request: {method}")

        if response.error:
            raise JsonRpcError(
                response.error.get("code", INTERNAL_ERROR),
                response.error.get("message", "unknown"),
                response.error.get("data"),
            )

        return response.result

    async def send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        await self._send_raw(msg)

    # ── Cancellation ──

    async def cancel_request(self, req_id: JsonRpcId) -> None:
        """Cancel an in-flight request."""
        await self.send_notification("notifications/cancelled", {
            "requestId": req_id,
            "reason": "User cancelled",
        })
        self._pending.pop(req_id, None)
        self._results.pop(req_id, None)

    # ── Read loop ──

    async def _read_loop(self) -> None:
        """Background task: reads JSON-RPC messages from the server's stdout."""
        while self._running and self._process and self._process.stdout:
            try:
                line_bytes = await self._process.stdout.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="replace")

                try:
                    msg = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue

                msg_id = msg.get("id")
                method = msg.get("method")

                # ── Response to our request ──
                if msg_id is not None and method is None:
                    if msg_id in self._pending:
                        response = JsonRpcResponse(
                            jsonrpc=msg.get("jsonrpc", "2.0"),
                            result=msg.get("result"),
                            error=msg.get("error"),
                            id=msg_id,
                        )
                        self._results[msg_id] = response
                        self._pending[msg_id].set()

                # ── Server → client request ──
                elif method and "id" in msg and msg["id"] is not None:
                    await self._handle_server_request(msg)

                # ── Notification from server ──
                elif method and "id" not in msg:
                    await self._handle_notification(method, msg.get("params", {}))

            except asyncio.CancelledError:
                raise
            except Exception:
                if self._running:
                    logger.exception("[%s] Read error", self.name)
                break

    async def _handle_server_request(self, msg: dict[str, Any]) -> None:
        """Handle a request from the server (e.g. sampling/createMessage)."""
        method = msg.get("method", "")
        req_id = msg.get("id")

        # For now, return method-not-found for all server requests.
        # Full sampling support would require an LLM callback from the agent.
        error_response = {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": METHOD_NOT_FOUND,
                "message": f"Method not supported by this client: {method}",
            },
        }
        await self._send_raw(error_response)

    async def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        """Handle a notification from the server."""
        if method == "notifications/progress":
            # Progress token + progress + total
            progress_token = params.get("progressToken")
            progress = params.get("progress", 0)
            total = params.get("total", 0)
            if self._on_progress:
                self._on_progress(progress, total, progress_token)
            logger.debug("[%s] Progress: %d/%d", self.name, progress, total)

        elif method == "notifications/resources/updated":
            uri = params.get("uri", "?")
            logger.info("[%s] Resource updated: %s", self.name, uri)

        elif method == "notifications/resources/list_changed":
            logger.info("[%s] Resource list changed — re-discovering", self.name)
            try:
                await self.discover()
            except Exception:
                pass

        elif method == "notifications/tools/list_changed":
            logger.info("[%s] Tool list changed — re-discovering", self.name)
            try:
                await self.discover()
            except Exception:
                pass

        elif method == "notifications/prompts/list_changed":
            logger.info("[%s] Prompt list changed — re-discovering", self.name)
            try:
                await self.discover()
            except Exception:
                pass

        elif method == "notifications/message":
            # Server→client log message
            level = params.get("level", "info")
            data = params.get("data", "")
            log_func = getattr(logger, level, logger.info)
            log_func("[%s] %s", self.name, data)


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP / SSE connection
# ═══════════════════════════════════════════════════════════════════════════════

class HTTPMCPConnection(MCPServerConnection):
    """MCP connection over HTTP (Streamable HTTP transport)."""

    def __init__(self, name: str, url: str, headers: dict[str, str] | None = None):
        super().__init__(name)
        self.url = url.rstrip("/")
        self._headers = headers or {}
        self._client: httpx.Client | None = None
        self._id_counter = 0

    def _next_id(self) -> str:
        self._id_counter += 1
        return str(self._id_counter)

    async def start(self) -> None:
        """Initialize HTTP connection."""
        self._client = httpx.Client(
            timeout=httpx.Timeout(120.0, connect=30.0),
            headers={
                "Content-Type": "application/json",
                **self._headers,
            },
        )
        logger.info("[%s] Connecting to %s", self.name, self.url)

        try:
            await self.initialize()
            await self.discover()
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        if self._client:
            self._client.close()
            self._client = None
        logger.info("[%s] Disconnected", self.name)

    def _post(self, msg: dict[str, Any]) -> httpx.Response:
        if not self._client:
            raise RuntimeError("MCP HTTP client not connected")
        response = self._client.post(self.url, json=msg)
        response.raise_for_status()
        return response

    async def send_request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": self._next_id(),
        }
        response = await asyncio.to_thread(self._post, msg)

        # Handle SSE stream for streaming responses
        # _read_sse iterates synchronously — run it in a thread to avoid
        # blocking the event loop on long-lived SSE connections.
        ct = response.headers.get("content-type", "")
        if "text/event-stream" in ct:
            return await asyncio.to_thread(self._read_sse, response)

        data = response.json()
        if data.get("error"):
            raise JsonRpcError(
                data["error"].get("code", INTERNAL_ERROR),
                data["error"].get("message", "unknown"),
                data["error"].get("data"),
            )
        return data.get("result")

    async def send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        await asyncio.to_thread(self._post, msg)

    @staticmethod
    def _read_sse(response: httpx.Response) -> Any:
        """Read SSE stream, collect the final result."""
        result = None
        for line in response.iter_lines():
            if line.startswith("data: "):
                try:
                    data = json.loads(line[6:])
                    if data.get("result") is not None:
                        result = data["result"]
                    if data.get("error"):
                        raise JsonRpcError(
                            data["error"].get("code", INTERNAL_ERROR),
                            data["error"].get("message", "unknown"),
                        )
                except json.JSONDecodeError:
                    continue
        return result


# ═══════════════════════════════════════════════════════════════════════════════
# MCP Client — manages multiple connections
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class MCPServerConfig:
    """Configuration for a single MCP server."""
    name: str
    transport: str = "stdio"
    # stdio config
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    # http config
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)


class MCPClient:
    """
    MCP client managing multiple MCP server connections.

    Discovers tools, resources, prompts from all servers.
    Provides unified search, caching, and health monitoring.
    """

    def __init__(self, servers: list[MCPServerConfig] | None = None):
        self._connections: dict[str, MCPServerConnection] = {}
        self._tool_to_server: dict[str, str] = {}
        self._all_tools: list[dict[str, Any]] = []
        self._resource_cache: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._resource_cache_max = 64
        self._resource_cache_ttl = 300.0
        self._health_task: asyncio.Task | None = None
        self._health_interval = 60.0
        self._health_running = False
        self._on_health_fail: Callable[[str], None] | None = None

        # Store servers passed to __init__ for deferred async connection.
        # add_server() is async — cannot be called from sync __init__.
        # Caller must await connect_all() to actually connect them.
        self._pending_servers: list[MCPServerConfig] = list(servers) if servers else []

    # ── Server lifecycle ───────────────────────────────────────────────────

    async def connect_all(self) -> int:
        """Connect all servers that were passed to __init__.

        Safe to call multiple times — subsequent calls are no-ops.
        Returns the number of servers successfully connected.
        """
        count = 0
        for cfg in self._pending_servers:
            try:
                await self.add_server(cfg)
                count += 1
            except Exception:
                logger.warning(
                    "Failed to connect MCP server '%s'", cfg.name, exc_info=True,
                )
        self._pending_servers.clear()
        return count

    async def add_server(self, config: MCPServerConfig) -> None:
        """Add and connect to an MCP server."""
        if config.transport == "stdio":
            conn = StdioMCPConnection(
                name=config.name,
                command=config.command,
                args=config.args,
                env=config.env or None,
                cwd=config.cwd or None,
            )
        elif config.transport == "http":
            conn = HTTPMCPConnection(
                name=config.name,
                url=config.url,
                headers=config.headers or None,
            )
        else:
            raise ValueError(f"Unknown transport: {config.transport}")

        try:
            await conn.start()
            self._connections[config.name] = conn
            self._register_server_tools(config.name, conn)
            logger.info(
                "Added MCP server '%s': %d tools, %d resources, %d prompts",
                config.name, len(conn.tools), len(conn.resources), len(conn.prompts),
            )
        except Exception:
            try:
                await conn.stop()
            except Exception:
                pass
            raise

    async def remove_server(self, name: str) -> None:
        """Disconnect and remove an MCP server."""
        conn = self._connections.pop(name, None)
        if conn:
            await conn.stop()
            self._all_tools = [t for t in self._all_tools if t.get("_mcp_server") != name]
            self._tool_to_server = {k: v for k, v in self._tool_to_server.items() if v != name}
            # Purge cache entries from this server
            self._resource_cache = OrderedDict(
                (k, v) for k, v in self._resource_cache.items()
                if not k.startswith(f"{name}:")
            )
            logger.info("Removed MCP server '%s'", name)

    async def stop_all(self) -> None:
        """Stop all MCP server connections."""
        await self._stop_health_monitor()
        for name, conn in list(self._connections.items()):
            try:
                await conn.stop()
            except Exception:
                pass
        self._connections.clear()
        self._all_tools.clear()
        self._tool_to_server.clear()
        self._resource_cache.clear()
        logger.info("All MCP servers stopped")

    # ── Tool registration ───────────────────────────────────────────────────

    def _register_server_tools(self, server_name: str, conn: MCPServerConnection) -> None:
        """Register tools from a server connection."""
        for tool in conn.tools:
            tool_name = tool["name"]
            prefixed = f"mcp__{server_name}__{tool_name}"
            if len(prefixed) > 64:
                suffix = tool_name[-30:] if len(tool_name) > 30 else tool_name
                prefixed = f"mcp__{server_name[:20]}__{suffix}"
                logger.warning("MCP tool name truncated: %s", prefixed)
            self._tool_to_server[prefixed] = server_name
            tool["_mcp_server"] = server_name
            tool["_mcp_original_name"] = tool_name
            self._all_tools.append(tool)

    async def refresh_tools(self, server_name: str | None = None) -> None:
        """Re-discover and re-register tools from one or all servers."""
        names = [server_name] if server_name else list(self._connections)
        for name in names:
            conn = self._connections.get(name)
            if not conn:
                continue
            # Remove old tools for this server
            self._all_tools = [t for t in self._all_tools if t.get("_mcp_server") != name]
            self._tool_to_server = {k: v for k, v in self._tool_to_server.items() if v != name}
            # Re-discover and register
            await conn.discover()
            self._register_server_tools(name, conn)

    # ── Tool access ─────────────────────────────────────────────────────────

    def get_tools(self) -> list[dict[str, Any]]:
        """Get all tools as OpenAI function tool definitions."""
        openai_tools = []
        for tool in self._all_tools:
            server = tool.get("_mcp_server", "?")
            original = tool.get("_mcp_original_name", tool.get("name", "?"))
            # Apply the SAME truncation logic as _register_server_tools()
            # so the name returned here matches the key in _tool_to_server.
            prefixed = f"mcp__{server}__{original}"
            if len(prefixed) > 64:
                suffix = original[-30:] if len(original) > 30 else original
                prefixed = f"mcp__{server[:20]}__{suffix}"
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": prefixed,
                    "description": tool.get("description", f"MCP tool: {tool['name']}"),
                    "parameters": tool.get("inputSchema", {
                        "type": "object", "properties": {},
                    }),
                },
            })
        return openai_tools

    async def call_tool(self, prefixed_name: str, arguments: dict[str, Any]) -> Any:
        """Call an MCP tool by its prefixed name."""
        server_name = self._tool_to_server.get(prefixed_name)
        if not server_name:
            raise ValueError(f"Unknown MCP tool: {prefixed_name}")

        conn = self._connections.get(server_name)
        if not conn:
            raise RuntimeError(f"MCP server not connected: {server_name}")

        for tool in self._all_tools:
            srv = tool.get("_mcp_server")
            original = tool.get("_mcp_original_name")
            if srv == server_name and f"mcp__{srv}__{original}" == prefixed_name:
                return await conn.call_tool(tool["_mcp_original_name"], arguments)

        raise ValueError(f"Tool not found: {prefixed_name}")

    def is_mcp_tool(self, tool_name: str) -> bool:
        return tool_name.startswith("mcp__") and tool_name in self._tool_to_server

    # ── Prompts ─────────────────────────────────────────────────────────────

    def list_prompts(self) -> list[dict[str, Any]]:
        """List all prompts from all servers."""
        result: list[dict[str, Any]] = []
        for name, conn in self._connections.items():
            for p in conn.prompts:
                result.append({**p, "_mcp_server": name})
        return result

    async def get_prompt(self, server: str, prompt_name: str,
                         arguments: dict[str, str] | None = None) -> Any:
        """Get a prompt from a specific server."""
        conn = self._connections.get(server)
        if not conn:
            raise ValueError(f"Server not found: {server}")
        return await conn.get_prompt(prompt_name, arguments)

    # ── Search ──────────────────────────────────────────────────────────────

    def search_tools(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Fuzzy search MCP tools across all servers."""
        q = query.lower().strip()
        if not q:
            return []

        scored: list[tuple[int, dict[str, Any]]] = []
        for tool in self._all_tools:
            name = tool.get("name", "")
            desc = tool.get("description", "")
            name_l = name.lower()
            score = 0
            if name_l == q:
                score = 3
            elif name_l.startswith(q):
                score = 2
            elif q in name_l:
                score = 1
            elif q in desc.lower():
                score = 0

            if q in name_l or q in desc.lower():
                scored.append((score, tool))

        scored.sort(key=lambda x: (-x[0], x[1].get("name", "")))
        return [t for _, t in scored[:limit]]

    def search_resources(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Search MCP resources by URI across all servers."""
        q = query.lower().strip()
        if not q:
            return []

        results: list[dict[str, Any]] = []
        for conn in self._connections.values():
            for res in conn.resources:
                uri = res.get("uri", "").lower()
                name = res.get("name", "").lower()
                desc = res.get("description", "").lower()
                if q in uri or q in name or q in desc:
                    results.append({**res, "_mcp_server": conn.name})
        results.sort(key=lambda r: r.get("name", r.get("uri", "")))
        return results[:limit]

    def get_all_resources(self) -> list[dict[str, Any]]:
        """Return all discovered resources from all servers."""
        results: list[dict[str, Any]] = []
        for conn in self._connections.values():
            for res in conn.resources:
                results.append({**res, "_mcp_server": conn.name})
        results.sort(key=lambda r: r.get("name", r.get("uri", "")))
        return results

    # ── Resource cache ──────────────────────────────────────────────────────

    async def cached_read_resource(self, uri: str) -> dict[str, Any]:
        """Read a resource with LRU+TTL caching."""
        now = time.time()
        if uri in self._resource_cache:
            content, ts = self._resource_cache[uri]
            if now - ts < self._resource_cache_ttl:
                self._resource_cache.move_to_end(uri)
                return {"content": content, "cached": True, "server": ""}
            del self._resource_cache[uri]

        # Find the owning server
        for conn in self._connections.values():
            for res in conn.resources:
                if res.get("uri") == uri:
                    result = await conn.read_resource(uri)
                    content = result.get("contents", result)
                    if len(self._resource_cache) >= self._resource_cache_max:
                        self._resource_cache.popitem(last=False)
                    self._resource_cache[uri] = (content, now)
                    return {"content": content, "cached": False, "server": conn.name}

        # Try resource templates
        for conn in self._connections.values():
            for tmpl in conn.resource_templates:
                tmpl_uri = tmpl.get("uriTemplate", "")
                # Simple match: if URI starts with the template prefix
                prefix = tmpl_uri.split("{")[0] if "{" in tmpl_uri else tmpl_uri
                if uri.startswith(prefix):
                    result = await conn.read_resource(uri)
                    content = result.get("contents", result)
                    if len(self._resource_cache) >= self._resource_cache_max:
                        self._resource_cache.popitem(last=False)
                    self._resource_cache[uri] = (content, now)
                    return {"content": content, "cached": False, "server": conn.name}

        raise ValueError(f"Resource not found on any server: {uri}")

    def invalidate_resource_cache(self, uri: str | None = None) -> None:
        """Invalidate cached resources."""
        if uri:
            self._resource_cache.pop(uri, None)
        else:
            self._resource_cache.clear()

    # ── Health monitoring ───────────────────────────────────────────────────

    def on_health_fail(self, callback: Callable[[str], None]) -> None:
        """Register a callback for health check failures."""
        self._on_health_fail = callback

    async def reconnect_server(self, name: str) -> bool:
        """Attempt to reconnect a failed MCP server. Returns True on success."""
        conn = self._connections.get(name)
        if not conn:
            return False
        logger.info("[%s] Attempting reconnection...", name)
        try:
            await conn.stop()
        except Exception:
            pass
        try:
            await conn.start()
            self._register_server_tools(name, conn)
            logger.info("[%s] Reconnected successfully", name)
            return True
        except Exception as e:
            logger.warning("[%s] Reconnection failed: %s", name, e)
            return False

    def start_health_monitor(self, interval: float = 60.0) -> None:
        """Start periodic health checks (ping every N seconds)."""
        if self._health_running:
            return
        self._health_interval = interval
        self._health_running = True
        self._health_task = asyncio.create_task(self._health_loop())
        logger.info("MCP health monitor started (interval=%.0fs)", interval)

    async def _stop_health_monitor(self) -> None:
        self._health_running = False
        if self._health_task and not self._health_task.done():
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
            self._health_task = None

    async def _health_loop(self) -> None:
        _failures: dict[str, int] = {}
        while self._health_running:
            await asyncio.sleep(self._health_interval)
            if not self._health_running:
                break
            for name, conn in list(self._connections.items()):
                try:
                    alive = await conn.ping(timeout=10)
                    if not alive:
                        logger.warning("[%s] Health check failed — attempting reconnect", name)
                        if self._on_health_fail:
                            self._on_health_fail(name)
                        await self.reconnect_server(name)
                    _failures.pop(name, None)  # reset on success
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    fails = _failures.get(name, 0) + 1
                    _failures[name] = fails
                    backoff = min(300, 5 * (2 ** (fails - 1)))  # 5, 10, 20, 40, 80, 160, 300...
                    logger.warning(
                        "[%s] Health check error (#%d): %s — will retry in %ds",
                        name, fails, e, backoff,
                    )
                    await asyncio.sleep(backoff)
                    if self._health_running:
                        if self._on_health_fail:
                            self._on_health_fail(name)
                        await self.reconnect_server(name)

    # ── Properties ──────────────────────────────────────────────────────────

    @property
    def connected_servers(self) -> list[str]:
        return list(self._connections.keys())

    @property
    def tool_count(self) -> int:
        return len(self._all_tools)

    @property
    def resource_count(self) -> int:
        return sum(len(c.resources) for c in self._connections.values())


# ═══════════════════════════════════════════════════════════════════════════════
# MCP config file support
# ═══════════════════════════════════════════════════════════════════════════════

def load_mcp_config(config_path: str | Path) -> list[MCPServerConfig]:
    """
    Load MCP server configurations from a JSON file.

    Example config.json:
    {
      "mcpServers": {
        "filesystem": {
          "transport": "stdio",
          "command": "npx",
          "args": ["-y", "@anthropic/mcp-filesystem", "/path/to/allowed"]
        },
        "github": {
          "transport": "stdio",
          "command": "npx",
          "args": ["-y", "@anthropic/mcp-github"],
          "env": {"GITHUB_TOKEN": "ghp_xxx"}
        },
        "remote-api": {
          "transport": "http",
          "url": "https://mcp.example.com/mcp",
          "headers": {"Authorization": "Bearer xxx"}
        }
      }
    }
    """
    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    servers = []
    for name, cfg in data.get("mcpServers", {}).items():
        servers.append(MCPServerConfig(
            name=name,
            transport=cfg.get("transport", "stdio"),
            command=cfg.get("command", ""),
            args=cfg.get("args", []),
            env=cfg.get("env", {}),
            cwd=cfg.get("cwd", ""),
            url=cfg.get("url", ""),
            headers=cfg.get("headers", {}),
        ))
    return servers
