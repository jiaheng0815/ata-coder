"""
MCP (Model Context Protocol) client for cross-system tool interoperability.

Supports MCP servers over:
- stdio (subprocess): spawns the server as a child process
- HTTP/SSE: connects to a remote MCP server

The client discovers tools from all connected MCP servers and makes them
available to the agent alongside built-in tools.

Spec: https://spec.modelcontextprotocol.io/
"""

import json
import logging
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

logger = logging.getLogger(__name__)


# ── JSON-RPC types ───────────────────────────────────────────────────────────

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


# ── MCP Server connection ────────────────────────────────────────────────────

class MCPServerConnection:
    """
    A connection to a single MCP server.

    Handles JSON-RPC communication, capability negotiation,
    and tool/resource discovery.
    """

    def __init__(self, name: str):
        self.name = name
        self._tools: list[dict[str, Any]] = []
        self._resources: list[dict[str, Any]] = []
        self._initialized: bool = False
        self._server_info: dict[str, Any] = {}
        self._capabilities: dict[str, Any] = {}

    @property
    def tools(self) -> list[dict[str, Any]]:
        return self._tools

    @property
    def resources(self) -> list[dict[str, Any]]:
        return self._resources

    @property
    def initialized(self) -> bool:
        return self._initialized

    def start(self) -> None:
        """Start the connection. Override in subclasses."""
        raise NotImplementedError

    def stop(self) -> None:
        """Stop the connection. Override in subclasses."""
        raise NotImplementedError

    def send_request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send a JSON-RPC request and return the result."""
        raise NotImplementedError

    def discover(self) -> None:
        """Discover tools and resources from this server."""
        try:
            # List tools
            result = self.send_request("tools/list", {})
            self._tools = result.get("tools", [])
            logger.info(
                "[%s] Discovered %d tools", self.name, len(self._tools)
            )

            # List resources (if supported)
            try:
                result = self.send_request("resources/list", {})
                self._resources = result.get("resources", [])
                logger.info(
                    "[%s] Discovered %d resources",
                    self.name,
                    len(self._resources),
                )
            except Exception:
                pass  # resources not supported by all servers

            self._initialized = True
        except Exception as e:
            logger.error("[%s] Discovery failed: %s", self.name, e)
            raise

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Call a tool on this MCP server."""
        result = self.send_request("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })
        return result

    def read_resource(self, uri: str) -> Any:
        """Read a resource from this MCP server."""
        result = self.send_request("resources/read", {"uri": uri})
        return result


class StdioMCPConnection(MCPServerConnection):
    """
    MCP connection over stdio (subprocess).

    Spawns the MCP server as a child process and communicates
    via JSON-RPC over stdin/stdout.
    """

    def __init__(self, name: str, command: str, args: list[str] | None = None,
                 env: dict[str, str] | None = None, cwd: str | None = None):
        super().__init__(name)
        self.command = command
        self.args = args or []
        self.env = env
        self.cwd = cwd
        self._process: subprocess.Popen | None = None
        self._pending: dict[JsonRpcId, threading.Event] = {}
        self._results: dict[JsonRpcId, JsonRpcResponse] = {}
        self._reader_thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.Lock()

    def start(self) -> None:
        """Start the MCP server process."""
        logger.info("[%s] Starting: %s %s", self.name, self.command, " ".join(self.args))

        try:
            self._process = subprocess.Popen(
                [self.command] + self.args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.env,
                cwd=self.cwd,
                bufsize=1,
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"MCP server command not found: {self.command}. "
                f"Install it or check the path."
            )
        except Exception as e:
            raise RuntimeError(f"Failed to start MCP server: {e}")

        self._running = True
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

        # Initialize the connection
        try:
            init_result = self.send_request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {},
                    "resources": {},
                },
                "clientInfo": {
                    "name": "ai-coder-agent",
                    "version": "1.0.0",
                },
            })
            self._server_info = init_result.get("serverInfo", {})
            self._capabilities = init_result.get("capabilities", {})

            # Send initialized notification
            self._send_notification("notifications/initialized", {})
            logger.info(
                "[%s] Initialized: %s",
                self.name,
                self._server_info.get("name", "unknown"),
            )
        except Exception as e:
            self.stop()
            raise RuntimeError(f"MCP initialization failed: {e}")

    def stop(self) -> None:
        """Stop the MCP server process."""
        self._running = False
        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None
        logger.info("[%s] Stopped", self.name)

    def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        self._send_raw(msg)

    def _send_raw(self, msg: dict[str, Any]) -> None:
        """Send a raw JSON-RPC message to the server."""
        if not self._process or not self._process.stdin:
            raise RuntimeError("MCP server not running")
        line = json.dumps(msg, ensure_ascii=False) + "\n"
        try:
            self._process.stdin.write(line)
            self._process.stdin.flush()
        except Exception as e:
            raise RuntimeError(f"Failed to send to MCP server: {e}")

    def send_request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send a JSON-RPC request and wait for the response."""
        req_id = str(uuid.uuid4())[:8]
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": req_id,
        }

        event = threading.Event()
        with self._lock:
            self._pending[req_id] = event

        self._send_raw(msg)

        # Wait for response with timeout
        if not event.wait(timeout=60):
            with self._lock:
                self._pending.pop(req_id, None)
            raise RuntimeError(f"MCP request timeout: {method}")

        with self._lock:
            response = self._results.pop(req_id, None)

        if response is None:
            raise RuntimeError(f"No response for request: {method}")

        if response.error:
            raise RuntimeError(
                f"MCP error: {response.error.get('message', 'unknown')}"
            )

        return response.result

    def _read_loop(self) -> None:
        """Background thread that reads JSON-RPC messages from the server."""
        while self._running and self._process and self._process.stdout:
            try:
                line = self._process.stdout.readline()
                if not line:
                    break

                try:
                    msg = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue

                # Handle responses
                msg_id = msg.get("id")
                if msg_id is not None:
                    with self._lock:
                        if msg_id in self._pending:
                            response = JsonRpcResponse(
                                jsonrpc=msg.get("jsonrpc", "2.0"),
                                result=msg.get("result"),
                                error=msg.get("error"),
                                id=msg_id,
                            )
                            self._results[msg_id] = response
                            self._pending[msg_id].set()

                # Handle server->client requests (like sampling)
                method = msg.get("method")
                if method and "id" in msg:
                    # For now, return an error for unsupported server requests
                    error_response = {
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "error": {
                            "code": -32601,
                            "message": f"Method not supported: {method}",
                        },
                    }
                    self._send_raw(error_response)

            except Exception:
                if self._running:
                    logger.exception("[%s] Read error", self.name)
                break

        # Release all pending requests on disconnect
        with self._lock:
            for req_id, event in self._pending.items():
                event.set()
            self._pending.clear()


class HTTPMCPConnection(MCPServerConnection):
    """
    MCP connection over HTTP (Streamable HTTP transport).

    Connects to a remote MCP server via HTTP POST for requests
    and SSE for streaming responses.
    """

    def __init__(self, name: str, url: str, headers: dict[str, str] | None = None):
        super().__init__(name)
        self.url = url.rstrip("/")
        self._headers = headers or {}
        self._client: httpx.Client | None = None

    def start(self) -> None:
        """Initialize HTTP connection."""
        self._client = httpx.Client(
            timeout=httpx.Timeout(60.0),
            headers={
                "Content-Type": "application/json",
                **self._headers,
            },
        )
        logger.info("[%s] Connecting to %s", self.name, self.url)

        # Initialize
        try:
            init_result = self.send_request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}, "resources": {}},
                "clientInfo": {
                    "name": "ai-coder-agent",
                    "version": "1.0.0",
                },
            })
            self._server_info = init_result.get("serverInfo", {})
            self._capabilities = init_result.get("capabilities", {})

            # Send initialized notification
            self._post({
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            })
            logger.info(
                "[%s] Initialized: %s",
                self.name,
                self._server_info.get("name", "unknown"),
            )
        except Exception as e:
            self.stop()
            raise RuntimeError(f"MCP HTTP initialization failed: {e}")

    def stop(self) -> None:
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

    def send_request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": str(uuid.uuid4())[:8],
        }
        response = self._post(msg)
        data = response.json()

        if data.get("error"):
            raise RuntimeError(
                f"MCP error: {data['error'].get('message', 'unknown')}"
            )
        return data.get("result")


# ── MCP Client (manages multiple connections) ────────────────────────────────

@dataclass
class MCPServerConfig:
    """Configuration for a single MCP server."""
    name: str
    transport: str = "stdio"  # "stdio" or "http"
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
    MCP client that manages multiple MCP server connections.

    Discovers tools from all servers and provides a unified interface
    for the agent to call them.
    """

    def __init__(self, servers: list[MCPServerConfig] | None = None):
        self._connections: dict[str, MCPServerConnection] = {}
        self._tool_to_server: dict[str, str] = {}  # tool_name -> server_name
        self._all_tools: list[dict[str, Any]] = []

        if servers:
            for cfg in servers:
                self.add_server(cfg)

    def add_server(self, config: MCPServerConfig) -> None:
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

        self._connections[config.name] = conn

        try:
            conn.start()
            conn.discover()

            # Register tools
            for tool in conn.tools:
                tool_name = tool["name"]
                prefixed = f"mcp__{config.name}__{tool_name}"
                # Truncate if too long (OpenAI limit: 64 chars for function name)
                if len(prefixed) > 64:
                    suffix = tool_name[-30:] if len(tool_name) > 30 else tool_name
                    prefixed = f"mcp__{config.name[:20]}__{suffix}"
                    logger.warning("MCP tool name truncated: %s", prefixed)
                self._tool_to_server[prefixed] = config.name
                tool["_mcp_server"] = config.name
                tool["_mcp_original_name"] = tool_name
                self._all_tools.append(tool)

            logger.info(
                "Added MCP server '%s': %d tools, %d resources",
                config.name,
                len(conn.tools),
                len(conn.resources),
            )
        except Exception as e:
            logger.error("Failed to add MCP server '%s': %s", config.name, e)
            try:
                conn.stop()
            except Exception:
                pass
            if config.name in self._connections:
                del self._connections[config.name]
            raise

    def remove_server(self, name: str) -> None:
        """Disconnect and remove an MCP server."""
        conn = self._connections.pop(name, None)
        if conn:
            conn.stop()
            # Remove its tools
            self._all_tools = [
                t for t in self._all_tools
                if t.get("_mcp_server") != name
            ]
            self._tool_to_server = {
                k: v for k, v in self._tool_to_server.items()
                if v != name
            }
            logger.info("Removed MCP server '%s'", name)

    def stop_all(self) -> None:
        """Stop all MCP server connections."""
        for name, conn in list(self._connections.items()):
            try:
                conn.stop()
            except Exception:
                pass
        self._connections.clear()
        self._all_tools.clear()
        self._tool_to_server.clear()
        logger.info("All MCP servers stopped")

    def get_tools(self) -> list[dict[str, Any]]:
        """Get all tools from all MCP servers as OpenAI tool definitions."""
        openai_tools = []
        for tool in self._all_tools:
            openai_tool = {
                "type": "function",
                "function": {
                    "name": f"mcp__{tool['_mcp_server']}__{tool['_mcp_original_name']}",
                    "description": tool.get("description", f"MCP tool: {tool['name']}"),
                    "parameters": tool.get("inputSchema", {
                        "type": "object",
                        "properties": {},
                    }),
                },
            }
            openai_tools.append(openai_tool)
        return openai_tools

    def call_tool(self, prefixed_name: str, arguments: dict[str, Any]) -> Any:
        """
        Call an MCP tool by its prefixed name.
        e.g., "mcp__myserver__my_tool"
        """
        server_name = self._tool_to_server.get(prefixed_name)
        if not server_name:
            raise ValueError(f"Unknown MCP tool: {prefixed_name}")

        conn = self._connections.get(server_name)
        if not conn:
            raise RuntimeError(f"MCP server not connected: {server_name}")

        # Extract original tool name
        for tool in self._all_tools:
            if (
                tool.get("_mcp_server") == server_name
                and f"mcp__{server_name}__{tool['_mcp_original_name']}" == prefixed_name
            ):
                return conn.call_tool(tool["_mcp_original_name"], arguments)

        raise ValueError(f"Tool not found: {prefixed_name}")

    def is_mcp_tool(self, tool_name: str) -> bool:
        """Check if a tool name is an MCP tool."""
        return tool_name.startswith("mcp__") and tool_name in self._tool_to_server

    @property
    def connected_servers(self) -> list[str]:
        return list(self._connections.keys())

    @property
    def tool_count(self) -> int:
        return len(self._all_tools)


# ── MCP config file support ──────────────────────────────────────────────────

def load_mcp_config(config_path: str | Path) -> list[MCPServerConfig]:
    """
    Load MCP server configurations from a JSON file.

    Example config.json:
    {
      "mcpServers": {
        "filesystem": {
          "transport": "stdio",
          "command": "npx",
          "args": ["-y", "@anthropic/mcp-filesystem", "/path/to/allowed"],
        },
        "github": {
          "transport": "stdio",
          "command": "npx",
          "args": ["-y", "@anthropic/mcp-github"],
          "env": {"GITHUB_TOKEN": "ghp_xxx"},
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
