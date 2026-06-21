/**
 * MCP Bridge — Model Context Protocol client in TypeScript.
 *
 * Replaces the Python mcp_client.py with:
 *  - Native TypeScript type safety for JSON-RPC protocol
 *  - Node.js 24's event loop for concurrent MCP connections
 *  - `using` for stdio subprocess cleanup
 *  - Global `fetch` (Undici 7) with HTTP/2 for HTTP transport
 *
 * Protocol: https://spec.modelcontextprotocol.io/
 */

import { spawn, type ChildProcess } from "node:child_process";
import { createInterface } from "node:readline";
import type { McpServerConfig } from "./types.ts";

// ── JSON-RPC 2.0 Types ──────────────────────────────────────────────────────

type JsonRpcId = string | number;

interface JsonRpcRequest {
  jsonrpc: "2.0";
  method: string;
  params?: Record<string, unknown>;
  id: JsonRpcId;
}

interface JsonRpcNotification {
  jsonrpc: "2.0";
  method: string;
  params?: Record<string, unknown>;
}

interface JsonRpcResponse {
  jsonrpc: "2.0";
  result?: unknown;
  error?: { code: number; message: string; data?: unknown };
  id: JsonRpcId;
}

// ── MCP Tool Definition ─────────────────────────────────────────────────────

interface McpTool {
  name: string;
  description?: string;
  inputSchema: {
    type: "object";
    properties: Record<string, unknown>;
    required?: string[];
  };
  _serverName: string;
  _originalName: string;
}

// ── MCP Connection (Abstract) ───────────────────────────────────────────────

abstract class McpConnection implements Disposable {
  readonly name: string;
  protected tools: McpTool[] = [];
  protected initialized = false;
  protected serverInfo: Record<string, unknown> = {};
  protected capabilities: Record<string, unknown> = {};
  readonly #logger: typeof console;

  constructor(name: string) {
    this.name = name;
    this.#logger = console;
  }

  abstract [Symbol.dispose](): void;
  abstract start(): Promise<void>;
  abstract sendRequest(method: string, params?: Record<string, unknown>): Promise<unknown>;
  abstract sendNotification(method: string, params?: Record<string, unknown>): Promise<void>;

  getTools(): McpTool[] { return this.tools; }
  isInitialized(): boolean { return this.initialized; }

  async initialize(capabilities?: Record<string, unknown>): Promise<void> {
    const result = await this.sendRequest("initialize", {
      protocolVersion: "2025-03-26",
      capabilities: {
        tools: {},
        resources: { subscribe: true },
        prompts: {},
        logging: {},
        ...capabilities,
      },
      clientInfo: { name: "ata-coder-ts", version: "2.5.2" },
    });

    const r = result as Record<string, unknown>;
    this.serverInfo = (r.serverInfo ?? {}) as Record<string, unknown>;
    this.capabilities = (r.capabilities ?? {}) as Record<string, unknown>;
    this.initialized = true;

    await this.sendNotification("notifications/initialized", {});
    this.#logger.info("[mcp:%s] Initialized: %s", this.name,
      (this.serverInfo as Record<string, string>)?.name ?? "unknown");
  }

  async discover(): Promise<void> {
    if (!this.initialized) throw new Error("Not initialized");

    if (this.capabilities.tools) {
      try {
        const result = await this.sendRequest("tools/list", {}) as Record<string, unknown>;
        const rawTools = (result.tools ?? []) as Array<Record<string, unknown>>;
        this.tools = rawTools.map((t) => ({
          name: t.name as string,
          description: t.description as string | undefined,
          inputSchema: (t.inputSchema ?? { type: "object", properties: {} }) as McpTool["inputSchema"],
          _serverName: this.name,
          _originalName: t.name as string,
        }));
        this.#logger.info("[mcp:%s] Discovered %d tools", this.name, this.tools.length);
      } catch (e) {
        this.#logger.warn("[mcp:%s] Tool discovery failed: %s", this.name, e);
      }
    }
  }

  async callTool(name: string, args: Record<string, unknown>): Promise<unknown> {
    return this.sendRequest("tools/call", { name, arguments: args });
  }
}

// ── Stdio Connection ────────────────────────────────────────────────────────

class StdioMcpConnection extends McpConnection {
  readonly #command: string;
  readonly #args: string[];
  readonly #env?: Record<string, string>;
  readonly #cwd?: string;
  #process?: ChildProcess;
  #nextId = 0;
  #pending = new Map<JsonRpcId, {
    resolve: (v: unknown) => void;
    reject: (e: Error) => void;
  }>();
  #running = false;

  constructor(config: McpServerConfig) {
    super(config.name);
    this.#command = config.command!;
    this.#args = config.args ?? [];
    this.#env = config.env;
    this.#cwd = config.cwd;
  }

  [Symbol.dispose](): void {
    this.#running = false;
    try { this.#process?.kill("SIGTERM"); } catch { /* ignore */ }
    for (const [, pr] of this.#pending) {
      pr.reject(new Error("Connection disposed"));
    }
    this.#pending.clear();
  }

  async start(): Promise<void> {
    this.#process = spawn(this.#command, this.#args, {
      stdio: ["pipe", "pipe", "pipe"],
      env: { ...process.env, ...this.#env },
      cwd: this.#cwd ?? process.cwd(),
    });

    this.#running = true;
    const rl = createInterface({
      input: this.#process.stdout!,
      crlfDelay: Infinity,
    });

    rl.on("line", (line: string) => {
      try {
        const msg = JSON.parse(line.trim()) as JsonRpcResponse;
        this.#handleMessage(msg);
      } catch { /* ignore unparsable lines */ }
    });

    this.#process.on("exit", (code) => {
      this.#running = false;
      for (const [, pr] of this.#pending) {
        pr.reject(new Error(`MCP server exited (code=${code})`));
      }
      this.#pending.clear();
    });

    await this.initialize();
    await this.discover();
  }

  async sendRequest(method: string, params?: Record<string, unknown>): Promise<unknown> {
    const id = ++this.#nextId;
    const req: JsonRpcRequest = {
      jsonrpc: "2.0",
      method,
      params: params ?? {},
      id,
    };

    return new Promise<unknown>((resolve, reject) => {
      this.#pending.set(id, { resolve, reject });
      const line = JSON.stringify(req) + "\n";
      this.#process?.stdin?.write(line);

      // Timeout after 60s
      setTimeout(() => {
        if (this.#pending.has(id)) {
          this.#pending.delete(id);
          reject(new Error(`MCP request timeout: ${method}`));
        }
      }, 60_000);
    });
  }

  async sendNotification(method: string, params?: Record<string, unknown>): Promise<void> {
    const msg: JsonRpcNotification = {
      jsonrpc: "2.0",
      method,
      params: params ?? {},
    };
    const line = JSON.stringify(msg) + "\n";
    this.#process?.stdin?.write(line);
  }

  #handleMessage(msg: JsonRpcResponse): void {
    if (msg.id === undefined || msg.id === null) return; // notification from server
    const pr = this.#pending.get(msg.id);
    if (!pr) return; // orphaned

    this.#pending.delete(msg.id);
    if (msg.error) {
      pr.reject(new Error(msg.error.message ?? "MCP error"));
    } else {
      pr.resolve(msg.result);
    }
  }
}

// ── HTTP Connection ─────────────────────────────────────────────────────────

class HttpMcpConnection extends McpConnection {
  readonly #url: string;
  readonly #headers: Record<string, string>;
  readonly #logger: typeof console;

  constructor(config: McpServerConfig) {
    super(config.name);
    this.#url = config.url!.replace(/\/$/, "");
    this.#headers = {
      "Content-Type": "application/json",
      ...config.headers,
    };
    this.#logger = console;
  }

  [Symbol.dispose](): void { /* HTTP is stateless — nothing to dispose */ }

  async start(): Promise<void> {
    this.#logger.info("[mcp:%s] Connecting to %s", this.name, this.#url);
    await this.initialize();
    await this.discover();
  }

  async sendRequest(method: string, params?: Record<string, unknown>): Promise<unknown> {
    const res = await fetch(this.#url, {
      method: "POST",
      headers: this.#headers,
      body: JSON.stringify({
        jsonrpc: "2.0",
        method,
        params: params ?? {},
        id: Date.now(),
      }),
    });

    if (!res.ok) {
      throw new Error(`MCP HTTP ${res.status}: ${await res.text().catch(() => "")}`);
    }

    const data = await res.json() as JsonRpcResponse;
    if (data.error) {
      throw new Error(data.error.message ?? "MCP error");
    }
    return data.result;
  }

  async sendNotification(method: string, params?: Record<string, unknown>): Promise<void> {
    await fetch(this.#url, {
      method: "POST",
      headers: this.#headers,
      body: JSON.stringify({
        jsonrpc: "2.0",
        method,
        params: params ?? {},
      }),
    });
  }
}

// ── MCP Bridge (manages multiple connections) ───────────────────────────────

export class McpBridge implements Disposable {
  readonly #connections = new Map<string, McpConnection>();
  #allTools: McpTool[] = [];
  readonly #toolToServer = new Map<string, string>();
  readonly #logger: typeof console;

  constructor() {
    this.#logger = console;
  }

  [Symbol.dispose](): void {
    for (const [, conn] of this.#connections) {
      conn[Symbol.dispose]();
    }
    this.#connections.clear();
    this.#allTools = [];
    this.#toolToServer.clear();
  }

  async connectAll(configs: McpServerConfig[]): Promise<number> {
    let count = 0;
    for (const cfg of configs) {
      try {
        await this.addServer(cfg);
        count++;
      } catch (e) {
        this.#logger.warn("[mcp] Failed to connect '%s': %s", cfg.name, e);
      }
    }
    return count;
  }

  async addServer(config: McpServerConfig): Promise<void> {
    const conn = config.transport === "http"
      ? new HttpMcpConnection(config)
      : new StdioMcpConnection(config);

    await conn.start();
    this.#connections.set(config.name, conn);
    this.#registerTools(config.name, conn);
    this.#logger.info("[mcp] Added server '%s': %d tools", config.name, conn.getTools().length);
  }

  // ── Shared: MCP tool name prefixing with 64-char truncation ────────────

  static #prefixName(server: string, tool: string): string {
    const p = `mcp__${server}__${tool}`;
    return p.length <= 64 ? p
      : `mcp__${server.slice(0, 20)}__${tool.length > 30 ? tool.slice(-30) : tool}`;
  }

  #registerTools(serverName: string, conn: McpConnection): void {
    for (const tool of conn.getTools()) {
      this.#toolToServer.set(McpBridge.#prefixName(serverName, tool.name), serverName);
      this.#allTools.push(tool);
    }
  }

  getOpenAiTools(): Array<{ type: string; function: { name: string; description: string; parameters: Record<string, unknown> } }> {
    return this.#allTools.map((t) => ({
      type: "function" as const,
      function: {
        name: McpBridge.#prefixName(t._serverName, t._originalName),
        description: t.description ?? `MCP tool: ${t.name}`,
        parameters: t.inputSchema as Record<string, unknown>,
      },
    }));
  }

  async callTool(prefixedName: string, args: Record<string, unknown>): Promise<unknown> {
    const server = this.#toolToServer.get(prefixedName);
    if (!server) throw new Error(`Unknown MCP tool: ${prefixedName}`);
    const conn = this.#connections.get(server);
    if (!conn) throw new Error(`MCP server not connected: ${server}`);
    const tool = this.#allTools.find((t) =>
      McpBridge.#prefixName(t._serverName, t._originalName) === prefixedName);
    if (!tool) throw new Error(`Tool not found: ${prefixedName}`);
    return conn.callTool(tool._originalName, args);
  }

  isMcpTool(name: string): boolean {
    return name.startsWith("mcp__") && this.#toolToServer.has(name);
  }

  get connections(): string[] {
    return [...this.#connections.keys()];
  }

  get toolCount(): number {
    return this.#allTools.length;
  }
}
