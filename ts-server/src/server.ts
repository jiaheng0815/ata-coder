/**
 * ATA Coder HTTP API Server — TypeScript Native (Node.js 24+)
 *
 * Replaces the Python server.py with:
 *  - Native TypeScript (node server.ts — no build step)
 *  - `using` for deterministic resource cleanup (subprocesses, PTY, timers)
 *  - AsyncLocalStorage for per-request trace context
 *  - V8 13.6: ~2× faster JSON.stringify on large payloads
 *  - node-pty for persistent shell sessions
 *  - Native SSE streaming via async generators
 *
 * Usage:
 *   node --experimental-transform-types src/server.ts --port 8080
 *   node --watch --experimental-transform-types src/server.ts
 *
 * The TypeScript server handles HTTP/SSE/shell/MCP concerns. Agent
 * execution is delegated to the Python core via subprocess IPC.
 * The Python ata_coder package remains the single source of truth
 * for all AI/LLM logic — this server is a companion, not a replacement.
 */

import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { randomUUID } from "node:crypto";
import { AgentBridge, requestContext } from "./agent-bridge.ts";
import { ShellManager } from "./shell-manager.ts";
import { McpBridge } from "./mcp-bridge.ts";
import type { ServerConfig, AgentResponse, StreamEvent } from "./types.ts";

// ── Runtime argument parsing (zero-dependency) ──────────────────────────────

function parseArgs(): ServerConfig {
  const args = process.argv.slice(2);
  return {
    port: parseInt(args[args.indexOf("--port") + 1] ?? "8080", 10),
    host: args[args.indexOf("--host") + 1] ?? "127.0.0.1",
    pythonPath: args[args.indexOf("--python") + 1] ?? "python",
    workspaceDir: args[args.indexOf("--workspace") + 1] ?? process.cwd(),
    maxConcurrentAgents: parseInt(args[args.indexOf("--max-agents") + 1] ?? "5", 10),
    sessionTtlSeconds: parseInt(args[args.indexOf("--session-ttl") + 1] ?? "3600", 10),
    shellTtlSeconds: parseInt(args[args.indexOf("--shell-ttl") + 1] ?? "3600", 10),
    maxThreads: parseInt(args[args.indexOf("--max-threads") + 1] ?? "50", 10),
    mcpServers: [],
  };
}

// ── SSE Helpers ─────────────────────────────────────────────────────────────

function sendSSE(res: ServerResponse, event: string, data: unknown): void {
  const payload = typeof data === "string" ? data : JSON.stringify(data);
  res.write(`event: ${event}\ndata: ${payload}\n\n`);
}

function sendSSEError(res: ServerResponse, message: string, status = 500): void {
  res.writeHead(status, { "Content-Type": "text/plain" });
  res.end(message);
}

// ── Server Implementation ───────────────────────────────────────────────────

class AtaCoderServer implements Disposable {
  readonly config: ServerConfig;
  readonly #bridge: AgentBridge;
  readonly #shells: ShellManager;
  readonly #mcp: McpBridge;
  #activeAgents = 0;

  constructor(config: ServerConfig) {
    this.config = config;
    this.#bridge = new AgentBridge(config.pythonPath, config.workspaceDir);
    this.#shells = new ShellManager(config.shellTtlSeconds);
    this.#mcp = new McpBridge();
  }

  [Symbol.dispose](): void {
    console.log("[server] Shutting down…");
    this.#bridge[Symbol.dispose]();
    this.#shells[Symbol.dispose]();
    this.#mcp[Symbol.dispose]();
    console.log("[server] All resources released.");
  }

  async start(): Promise<void> {
    // Connect MCP servers if configured
    if (this.config.mcpServers.length > 0) {
      const count = await this.#mcp.connectAll(this.config.mcpServers);
      console.log("[server] MCP servers connected: %d", count);
    }

    const httpServer = createServer((req, res) => {
      this.#handleRequest(req, res);
    });

    httpServer.on("error", (err) => {
      console.error("[server] HTTP error:", err.message);
    });

    await new Promise<void>((resolve) => {
      httpServer.listen(this.config.port, this.config.host, () => {
        console.log("[server] ATA Coder HTTP API v2.5.2 (TypeScript/Native)");
        console.log("[server] Listening on http://%s:%d", this.config.host, this.config.port);
        console.log("[server] Node.js %s — Native TypeScript mode", process.version);
        resolve();
      });
    });
  }

  // ── Request Routing ───────────────────────────────────────────────────────

  #handleRequest(req: IncomingMessage, res: ServerResponse): void {
    const traceId = randomUUID();
    const url = new URL(req.url ?? "/", `http://${req.headers.host ?? "localhost"}`);

    // CORS headers
    res.setHeader("Access-Control-Allow-Origin", "*");
    res.setHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
    res.setHeader("Access-Control-Allow-Headers", "Content-Type, Authorization");

    if (req.method === "OPTIONS") {
      res.writeHead(204);
      res.end();
      return;
    }

    // Concurrency guard
    if (this.#activeAgents >= this.config.maxThreads && url.pathname === "/v1/chat") {
      const body = JSON.stringify({ error: "Server busy - too many concurrent requests" });
      res.writeHead(503, {
        "Content-Type": "application/json",
        "Content-Length": String(Buffer.byteLength(body)),
      });
      res.end(body);
      return;
    }

    // Route within AsyncLocalStorage context
    requestContext.run({ traceId }, () => {
      try {
        switch (true) {
          case url.pathname === "/health" && req.method === "GET":
            return this.#handleHealth(req, res);
          case url.pathname === "/v1/chat" && req.method === "POST":
            return this.#handleChat(req, res);
          case url.pathname === "/v1/sessions" && req.method === "GET":
            return this.#handleListSessions(req, res);
          case url.pathname.startsWith("/v1/sessions/") && req.method === "DELETE":
            return this.#handleDeleteSession(req, res, url);
          case url.pathname === "/v1/shell/open" && req.method === "POST":
            return this.#handleShellOpen(req, res);
          case url.pathname === "/v1/shell/exec" && req.method === "POST":
            return this.#handleShellExec(req, res);
          case url.pathname === "/v1/shell/close" && req.method === "POST":
            return this.#handleShellClose(req, res);
          case url.pathname === "/v1/mcp/tools" && req.method === "GET":
            return this.#handleMcpTools(req, res);
          default:
            res.writeHead(404, { "Content-Type": "application/json" });
            res.end(JSON.stringify({ error: "Not found" }));
        }
      } catch (err) {
        console.error("[server] Unhandled error:", err);
        if (!res.headersSent) {
          res.writeHead(500, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ error: "Internal server error" }));
        }
      }
    });
  }

  // ── Health ────────────────────────────────────────────────────────────────

  #handleHealth(_req: IncomingMessage, res: ServerResponse): void {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({
      status: "ok",
      version: "2.5.2",
      runtime: "node",
      nodeVersion: process.version,
      activeAgents: this.#activeAgents,
      mcpServers: this.#mcp.connections.length,
      mcpTools: this.#mcp.toolCount,
    }));
  }

  // ── Chat (SSE streaming) ──────────────────────────────────────────────────

  async #handleChat(req: IncomingMessage, res: ServerResponse): Promise<void> {
    // Parse body
    const body = await this.#readBody(req);
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(body);
    } catch {
      res.writeHead(400, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "Invalid JSON" }));
      return;
    }

    const task = (parsed.task ?? parsed.prompt ?? "") as string;
    if (!task) {
      res.writeHead(400, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "Missing 'task' field" }));
      return;
    }

    const stream = parsed.stream !== false; // default: true

    if (!stream) {
      // Non-streaming
      try {
        this.#activeAgents++;
        const result = await this.#bridge.runTask(task, {
          skill: parsed.skill as string | undefined,
          model: parsed.model as string | undefined,
          sessionId: parsed.session_id as string | undefined,
          resetContext: parsed.reset_context !== false,
        });
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ text: result.text, error: result.error }));
      } catch (err) {
        res.writeHead(500, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: String(err) }));
      } finally {
        this.#activeAgents--;
      }
      return;
    }

    // SSE Streaming
    res.writeHead(200, {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no",
    });

    this.#activeAgents++;
    const sessionId = randomUUID();

    try {
      for await (const event of this.#bridge.runTaskStream(task, {
        skill: parsed.skill as string | undefined,
        model: parsed.model as string | undefined,
        sessionId,
        resetContext: parsed.reset_context !== false,
      })) {
        // Event is already yielded — send SSE
        sendSSE(res, event.type, event);
      }

      sendSSE(res, "done", { session_id: sessionId });
    } catch (err) {
      sendSSE(res, "error", { message: String(err) });
    } finally {
      this.#activeAgents--;
      res.end();
    }
  }

  // ── Session Management ────────────────────────────────────────────────────

  #handleListSessions(_req: IncomingMessage, res: ServerResponse): void {
    // Stub: list sessions from Python agent (requires IPC extension)
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ sessions: [] }));
  }

  #handleDeleteSession(_req: IncomingMessage, res: ServerResponse, url: URL): void {
    const sessionId = url.pathname.split("/").pop();
    // Stub: delete session via Python agent IPC
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ deleted: sessionId }));
  }

  // ── Shell Management ──────────────────────────────────────────────────────

  async #handleShellOpen(req: IncomingMessage, res: ServerResponse): Promise<void> {
    const body = await this.#readBody(req);
    let parsed: { cwd?: string; sid?: string; kind?: "powershell" | "bash" | "cmd" };
    try {
      parsed = JSON.parse(body);
    } catch {
      res.writeHead(400, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "Invalid JSON" }));
      return;
    }

    const session = this.#shells.open(parsed.cwd ?? this.config.workspaceDir, {
      sid: parsed.sid,
      kind: parsed.kind ?? "cmd",
    });

    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify(session));
  }

  async #handleShellExec(req: IncomingMessage, res: ServerResponse): Promise<void> {
    const body = await this.#readBody(req);
    let parsed: { sid?: string; command?: string; timeout?: number };
    try {
      parsed = JSON.parse(body);
    } catch {
      res.writeHead(400, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "Invalid JSON" }));
      return;
    }

    if (!parsed.sid || !parsed.command) {
      res.writeHead(400, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "Missing 'sid' or 'command'" }));
      return;
    }

    try {
      const result = await this.#shells.exec(parsed.sid, parsed.command, parsed.timeout);
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify(result));
    } catch (err) {
      res.writeHead(404, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: String(err) }));
    }
  }

  async #handleShellClose(req: IncomingMessage, res: ServerResponse): Promise<void> {
    const body = await this.#readBody(req);
    let parsed: { sid?: string };
    try {
      parsed = JSON.parse(body);
    } catch {
      res.writeHead(400, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "Invalid JSON" }));
      return;
    }

    if (!parsed.sid) {
      res.writeHead(400, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "Missing 'sid'" }));
      return;
    }

    this.#shells.close(parsed.sid);
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ closed: parsed.sid }));
  }

  // ── MCP Tools ─────────────────────────────────────────────────────────────

  #handleMcpTools(_req: IncomingMessage, res: ServerResponse): void {
    const tools = this.#mcp.getOpenAiTools();
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ tools, count: tools.length }));
  }

  // ── Helpers ───────────────────────────────────────────────────────────────

  #readBody(req: IncomingMessage): Promise<string> {
    return new Promise<string>((resolve, reject) => {
      const chunks: Buffer[] = [];
      req.on("data", (chunk: Buffer) => chunks.push(chunk));
      req.on("end", () => resolve(Buffer.concat(chunks).toString("utf-8")));
      req.on("error", reject);
    });
  }
}

// ── Entry Point ─────────────────────────────────────────────────────────────

if (process.argv[1]?.endsWith("server.ts") || process.argv[1]?.endsWith("server")) {
  const config = parseArgs();
  using server = new AtaCoderServer(config);
  await server.start();

  // Graceful shutdown
  const shutdown = (signal: string) => {
    console.log("\n[server] Received %s — shutting down gracefully…", signal);
    server[Symbol.dispose]();
    process.exit(0);
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);
}
