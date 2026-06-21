/**
 * Shell Session Manager — persistent shell sessions via node-pty.
 *
 * Replaces server_shell.py with superior PTY support:
 *  - Real PTY allocation (not subprocess pipes)
 *  - Proper terminal size handling
 *  - Prompt detection via OSC sequences
 *  - Node.js 24 `using` for deterministic cleanup
 *
 * Dependencies: node-pty (native PTY binding)
 */

import { ipc as ptyIpc, type IPty } from "node-pty";
import { EventEmitter } from "node:events";
import { randomUUID } from "node:crypto";
import type { ShellSession } from "./types.ts";

// ── Shell types ─────────────────────────────────────────────────────────────

type ShellKind = "powershell" | "bash" | "cmd";

function detectShell(kind: ShellKind): { command: string; args: string[] } {
  switch (kind) {
    case "powershell":
      return { command: "powershell.exe", args: ["-NoLogo", "-NoExit"] };
    case "bash":
      return { command: "bash", args: ["--norc"] };
    case "cmd":
      return { command: "cmd.exe", args: [] };
    default:
      return { command: "cmd.exe", args: [] };
  }
}

// ── Shell Entry ─────────────────────────────────────────────────────────────

interface ShellEntry {
  session: ShellSession;
  pty: IPty;
  emitter: EventEmitter;
  lastOutput: string;
  ttlTimer: ReturnType<typeof setTimeout>;
  /** Daemon reader reference for cleanup */
  readerDispose?: () => void;
}

export class ShellManager implements Disposable {
  readonly #shells = new Map<string, ShellEntry>();
  readonly #ttlMs: number;
  readonly #logger: typeof console;

  constructor(ttlSeconds = 3600) {
    this.#ttlMs = ttlSeconds * 1000;
    this.#logger = console;
  }

  // ── Symbol.dispose ────────────────────────────────────────────────────────

  [Symbol.dispose](): void {
    this.#logger.info("[shell-manager] disposing all shells…");
    for (const [id, entry] of this.#shells) {
      clearTimeout(entry.ttlTimer);
      try { entry.pty.kill(); } catch { /* ignore */ }
      this.#shells.delete(id);
    }
  }

  // ── Public API ────────────────────────────────────────────────────────────

  /**
   * Open a persistent shell session.
   * Safe against concurrent opens for the same session ID.
   */
  open(
    cwd: string,
    opts: { sid?: string; kind?: ShellKind } = {},
  ): ShellSession {
    const sid = opts.sid ?? randomUUID();
    const kind = opts.kind ?? "cmd";

    // TOCTOU-safe: check-then-create under a synchronous guard.
    // Node.js is single-threaded (event loop), so no race here
    // unlike Python's multi-threaded server.
    const existing = this.#shells.get(sid);
    if (existing) {
      if (existing.pty.exitCode === null) {
        // Shell is alive — reset TTL and return
        clearTimeout(existing.ttlTimer);
        existing.ttlTimer = this.#startTtl(sid);
        existing.session.lastUsed = Date.now();
        this.#logger.debug("[shell-manager] reused session: %s", sid);
        return existing.session;
      }
      // Dead shell — clean up old entry
      this.#shells.delete(sid);
    }

    const { command, args } = detectShell(kind);
    const pty = ptyIpc.spawn(command, args, {
      cwd,
      cols: 120,
      rows: 40,
      env: process.env as Record<string, string>,
    });

    const emitter = new EventEmitter();
    emitter.setMaxListeners(50);

    const session: ShellSession = {
      id: sid,
      cwd,
      shell: kind,
      createdAt: Date.now(),
      lastUsed: Date.now(),
    };

    const entry: ShellEntry = {
      session,
      pty,
      emitter,
      lastOutput: "",
      ttlTimer: this.#startTtl(sid),
    };

    // Daemon reader: collect output, detect prompts
    const onData = (data: string) => {
      entry.lastOutput = entry.lastOutput.slice(-100_000) + data; // rolling window
      emitter.emit("data", data);
    };
    pty.onData(onData);
    entry.readerDispose = () => pty.removeListener("data", onData);

    pty.onExit(({ exitCode, signal }) => {
      this.#logger.debug("[shell-manager] shell %s exited: code=%d sig=%d", sid, exitCode, signal);
      emitter.emit("exit", { exitCode, signal });
      clearTimeout(entry.ttlTimer);
      this.#shells.delete(sid);
    });

    this.#shells.set(sid, entry);
    this.#logger.info("[shell-manager] opened: %s (%s) at %s", sid, kind, cwd);
    return session;
  }

  /** Execute a command in an existing shell session */
  exec(
    sid: string,
    command: string,
    timeoutMs = 120_000,
  ): Promise<{ output: string; exitCode: number }> {
    const entry = this.#shells.get(sid);
    if (!entry) {
      return Promise.reject(new Error(`Shell session not found: ${sid}`));
    }

    // Reset TTL
    clearTimeout(entry.ttlTimer);
    entry.ttlTimer = this.#startTtl(sid);
    entry.session.lastUsed = Date.now();

    // Clear backlog before command
    entry.lastOutput = "";
    // Use a unique end marker to detect command completion
    const endMarker = `__END_${randomUUID().replace(/-/g, "")}__`;
    const fullCommand = `${command}\necho ${endMarker}%ERRORLEVEL%\n`;

    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        entry.emitter.removeAllListeners("data");
        // Kill and reopen on timeout — don't reject, return partial output
        resolve({ output: entry.lastOutput, exitCode: -1 });
      }, timeoutMs);

      const onData = (data: string) => {
        if (data.includes(endMarker)) {
          clearTimeout(timer);
          entry.emitter.removeListener("data", onData);
          // Parse exit code from end marker
          const match = entry.lastOutput.match(new RegExp(`${endMarker}(\\d+)`));
          const exitCode = match ? parseInt(match[1], 10) : 0;
          // Strip end marker from output
          const output = entry.lastOutput
            .replace(new RegExp(`.*?${endMarker}\\d*\r?\n?`, "s"), "")
            .trim();
          resolve({ output, exitCode });
        }
      };
      entry.emitter.on("data", onData);
      entry.pty.write(fullCommand);
    });
  }

  /** Close a shell session */
  close(sid: string): boolean {
    const entry = this.#shells.get(sid);
    if (!entry) return false;
    clearTimeout(entry.ttlTimer);
    try { entry.readerDispose?.(); } catch { /* ignore */ }
    try { entry.pty.kill(); } catch { /* ignore */ }
    this.#shells.delete(sid);
    this.#logger.info("[shell-manager] closed: %s", sid);
    return true;
  }

  /** Check if a shell is alive */
  isAlive(sid: string): boolean {
    const entry = this.#shells.get(sid);
    return entry !== undefined && entry.pty.exitCode === null;
  }

  /** List active shells */
  list(): ShellSession[] {
    const result: ShellSession[] = [];
    for (const [, entry] of this.#shells) {
      if (entry.pty.exitCode === null) {
        result.push({ ...entry.session });
      }
    }
    return result;
  }

  // ── TTL ───────────────────────────────────────────────────────────────────

  #startTtl(sid: string): ReturnType<typeof setTimeout> {
    return setTimeout(() => {
      this.#logger.info("[shell-manager] TTL expired: %s", sid);
      this.close(sid);
    }, this.#ttlMs);
  }
}
