/**
 * Agent Bridge — communicates with the Python ATA Coder agent core
 * via subprocess JSON-RPC over stdin/stdout.
 *
 * Node.js 24 features:
 *  - `using` for SyncDisposable cleanup of child processes
 *  - `Symbol.dispose` on the bridge class
 *  - AsyncLocalStorage for request tracing
 *  - V8 13.6: ~2× faster JSON.stringify on large tool results
 */

import { spawn, type ChildProcess } from "node:child_process";
import { createInterface } from "node:readline";
import { AsyncLocalStorage } from "node:async_hooks";
import { randomUUID } from "node:crypto";
import { EventEmitter } from "node:events";
import type {
  AgentRequest,
  AgentResponse,
  StreamEvent,
  TokenUsage,
} from "./types.ts";

// ── Request context (trace ID propagation) ──────────────────────────────────

export const requestContext = new AsyncLocalStorage<{ traceId: string }>();

// ── Bridge Implementation ───────────────────────────────────────────────────

interface PendingRequest {
  resolve: (value: AgentResponse) => void;
  reject: (reason: Error) => void;
  /** In streaming mode, each event is forwarded via this emitter */
  streamEmitter?: EventEmitter;
  timeout: ReturnType<typeof setTimeout>;
}

export class AgentBridge implements Disposable {
  readonly #process: ChildProcess;
  readonly #pending = new Map<string, PendingRequest>();
  readonly #pythonPath: string;
  readonly #workspaceDir: string;
  readonly #logger: typeof console;

  constructor(pythonPath: string, workspaceDir: string) {
    this.#pythonPath = pythonPath;
    this.#workspaceDir = workspaceDir;
    this.#logger = console;

    this.#process = spawn(pythonPath, ["-m", "ata_coder", "_ipc"], {
      cwd: workspaceDir,
      stdio: ["pipe", "pipe", "pipe"],
      env: { ...process.env, PYTHONUNBUFFERED: "1" },
    });

    // Line-buffered JSON reader on stdout
    const rl = createInterface({
      input: this.#process.stdout!,
      crlfDelay: Infinity,
    });

    rl.on("line", (line: string) => {
      try {
        const msg: AgentResponse = JSON.parse(line.trim());
        this.#handleResponse(msg);
      } catch {
        this.#logger.debug("[agent-bridge] unparsable line: %s", line.slice(0, 80));
      }
    });

    this.#process.stderr?.on("data", (chunk: Buffer) => {
      this.#logger.debug("[agent-bridge:stderr] %s", chunk.toString().trim());
    });

    this.#process.on("exit", (code, signal) => {
      this.#logger.warn("[agent-bridge] Python process exited: code=%d signal=%s", code, signal);
      // Reject all pending requests
      for (const [id, pr] of this.#pending) {
        clearTimeout(pr.timeout);
        pr.reject(new Error(`Agent process exited (code=${code}, signal=${signal})`));
        this.#pending.delete(id);
      }
    });
  }

  // ── Symbol.dispose (Node.js 24 `using` keyword) ──────────────────────────

  [Symbol.dispose](): void {
    this.#logger.info("[agent-bridge] disposing…");
    try {
      this.#process.stdin?.end();
    } catch { /* ignore */ }
    try {
      this.#process.kill("SIGTERM");
    } catch { /* ignore */ }
    // Clear all pending
    for (const [id, pr] of this.#pending) {
      clearTimeout(pr.timeout);
      pr.reject(new Error("Bridge disposed"));
      this.#pending.delete(id);
    }
  }

  // ── Public API ────────────────────────────────────────────────────────────

  /**
   * Run an agent task (non-streaming).
   * Returns the final response.
   */
  async runTask(
    task: string,
    opts: { skill?: string; model?: string; sessionId?: string; resetContext?: boolean } = {},
  ): Promise<AgentResponse> {
    const req: AgentRequest = {
      id: randomUUID(),
      op: "run",
      task,
      stream: false,
      ...opts,
    };
    return this.#sendAndWait(req, 300_000);
  }

  /**
   * Run an agent task with SSE-style streaming.
   * Each stream event is emitted on the returned EventEmitter.
   * Resolves when the agent completes.
   */
  async *runTaskStream(
    task: string,
    opts: { skill?: string; model?: string; sessionId?: string; resetContext?: boolean } = {},
  ): AsyncGenerator<StreamEvent, string, void> {
    const req: AgentRequest = {
      id: randomUUID(),
      op: "run",
      task,
      stream: true,
      ...opts,
    };

    const events: StreamEvent[] = [];
    let finished = false;
    let finalText = "";

    // Notify the generator loop whenever a new event arrives OR the request completes.
    let notifyFn: (() => void) | null = null;
    const notify = () => {
      if (notifyFn) { notifyFn(); notifyFn = null; }
    };

    const emitter = new EventEmitter();
    emitter.on("event", (evt: StreamEvent) => {
      events.push(evt);
      notify();
    });

    // Fire-and-forget: #send resolves/rejects on final response.
    // We use notify() to wake the generator loop for both events and completion.
    this.#send(req,
      (result: AgentResponse) => {
        finished = true;
        finalText = result.text ?? result.error ?? "";
        notify();
      },
      (err: Error) => {
        finished = true;
        finalText = err.message;
        notify();
      },
      600_000,
      emitter,
    );

    // Yield events as they arrive — don't wait for the agent to finish first.
    while (!finished || events.length > 0) {
      if (events.length > 0) {
        yield events.shift()!;
      } else if (!finished) {
        await new Promise<void>((resolve) => { notifyFn = resolve; });
      }
    }

    emitter.removeAllListeners("event");
    return finalText;
  }

  /** Cancel a running task */
  cancelTask(taskId: string): void {
    const req: AgentRequest = { id: taskId, op: "cancel" };
    this.#sendRaw(req);
  }

  /** Check agent health */
  async healthCheck(): Promise<boolean> {
    try {
      const resp = await this.#sendAndWait(
        { id: randomUUID(), op: "status" },
        5_000,
      );
      return resp.status !== "error";
    } catch {
      return false;
    }
  }

  /** Graceful shutdown */
  async shutdown(): Promise<void> {
    this.#sendRaw({ id: randomUUID(), op: "shutdown" });
    await new Promise((r) => setTimeout(r, 500));
    this[Symbol.dispose]();
  }

  // ── Internals ─────────────────────────────────────────────────────────────

  #sendRaw(msg: AgentRequest): void {
    const line = JSON.stringify(msg) + "\n";
    this.#process.stdin?.write(line);
  }

  #send(
    msg: AgentRequest,
    resolve: (v: AgentResponse) => void,
    reject: (e: Error) => void,
    timeoutMs: number,
    streamEmitter?: EventEmitter,
  ): void {
    const timeout = setTimeout(() => {
      this.#pending.delete(msg.id);
      reject(new Error(`Request timed out: ${msg.op} (${msg.id})`));
    }, timeoutMs);

    this.#pending.set(msg.id, { resolve, reject, streamEmitter, timeout });
    this.#sendRaw(msg);
  }

  async #sendAndWait(
    msg: AgentRequest,
    timeoutMs: number,
  ): Promise<AgentResponse> {
    return new Promise<AgentResponse>((resolve, reject) => {
      this.#send(msg, resolve, reject, timeoutMs);
    });
  }

  #handleResponse(msg: AgentResponse): void {
    const pr = this.#pending.get(msg.id);
    if (!pr) {
      this.#logger.debug("[agent-bridge] orphaned response: %s", msg.id);
      return;
    }

    if (msg.status === "stream" && msg.event && pr.streamEmitter) {
      // Forward stream event — don't resolve yet
      pr.streamEmitter.emit("event", msg.event);
      return;
    }

    // Final response
    clearTimeout(pr.timeout);
    this.#pending.delete(msg.id);
    pr.resolve(msg);
  }
}
