/**
 * Session Store — replaces server_session.py.
 *
 * CRUD for agent conversation sessions with TTL eviction.
 * Thread-safe (Node.js single-threaded event loop, so no locks needed).
 */

import { readFileSync, writeFileSync, existsSync, mkdirSync, readdirSync, unlinkSync, statSync, renameSync } from "node:fs";
import { join, basename } from "node:path";
import { randomUUID } from "node:crypto";
import { SESSIONS_DIR } from "./config.ts";

// ── Types ───────────────────────────────────────────────────────────────────

export interface SessionMessage {
  role: "system" | "user" | "assistant" | "tool";
  content: string | null;
  tool_calls?: Array<Record<string, unknown>>;
  tool_call_id?: string;
  name?: string;
}

export interface Session {
  id: string;
  created: string; // ISO 8601
  lastActive: string;
  messageCount: number;
  model: string;
  skill?: string;
  messages: SessionMessage[];
  toolCallCount: number;
}

interface SessionIndexEntry {
  id: string;
  created: string;
  lastActive: string;
  messageCount: number;
  model: string;
  skill?: string;
}

// ── Store Implementation ────────────────────────────────────────────────────

export class SessionStore implements Disposable {
  readonly #dir: string;
  readonly #index: Map<string, SessionIndexEntry> = new Map();
  readonly #ttlMs: number;
  #indexFile: string;
  #cleanupTimer?: ReturnType<typeof setInterval>;

  constructor(ttlSeconds = 3600) {
    this.#dir = SESSIONS_DIR;
    this.#indexFile = join(this.#dir, "_index.json");
    this.#ttlMs = ttlSeconds * 1000;
    this.#loadIndex();
    this.#startCleanup();
  }

  [Symbol.dispose](): void {
    if (this.#cleanupTimer) clearInterval(this.#cleanupTimer);
    this.#saveIndex();
  }

  // ── CRUD ──────────────────────────────────────────────────────────────────

  create(model: string, skill?: string): Session {
    const id = randomUUID();
    const now = new Date().toISOString();
    const session: Session = {
      id,
      created: now,
      lastActive: now,
      messageCount: 0,
      model,
      skill,
      messages: [],
      toolCallCount: 0,
    };
    this.#index.set(id, { id, created: now, lastActive: now, messageCount: 0, model, skill });
    this.#writeSession(session);
    return session;
  }

  get(id: string): Session | null {
    const entry = this.#index.get(id);
    if (!entry) return null;
    const path = this.#sessionPath(id);
    try {
      if (!existsSync(path)) return null;
      const session = JSON.parse(readFileSync(path, "utf-8")) as Session;
      // Update lastActive
      entry.lastActive = new Date().toISOString();
      session.lastActive = entry.lastActive;
      return session;
    } catch {
      return null;
    }
  }

  save(session: Session): void {
    session.lastActive = new Date().toISOString();
    session.messageCount = session.messages.length;
    const entry = this.#index.get(session.id);
    if (entry) {
      entry.lastActive = session.lastActive;
      entry.messageCount = session.messageCount;
      entry.model = session.model;
      entry.skill = session.skill;
    } else {
      this.#index.set(session.id, {
        id: session.id,
        created: session.created,
        lastActive: session.lastActive,
        messageCount: session.messageCount,
        model: session.model,
        skill: session.skill,
      });
    }
    this.#writeSession(session);
  }

  delete(id: string): boolean {
    const entry = this.#index.get(id);
    if (!entry) return false;
    this.#index.delete(id);
    try {
      const path = this.#sessionPath(id);
      if (existsSync(path)) unlinkSync(path);
    } catch {
      // best-effort
    }
    return true;
  }

  list(): SessionIndexEntry[] {
    return [...this.#index.values()]
      .sort((a, b) => b.lastActive.localeCompare(a.lastActive));
  }

  // ── TTL Eviction ──────────────────────────────────────────────────────────

  #startCleanup(): void {
    this.#cleanupTimer = setInterval(() => {
      const cutoff = Date.now() - this.#ttlMs;
      for (const [id, entry] of this.#index) {
        if (new Date(entry.lastActive).getTime() < cutoff) {
          this.delete(id);
        }
      }
    }, 60_000); // every minute
    this.#cleanupTimer.unref(); // don't keep process alive
  }

  // ── Persistence ───────────────────────────────────────────────────────────

  #sessionPath(id: string): string {
    return join(this.#dir, `${id}.json`);
  }

  #writeSession(session: Session): void {
    try {
      if (!existsSync(this.#dir)) mkdirSync(this.#dir, { recursive: true });
      // Atomic write: write to .tmp, then renameSync (Node.js 18+ supports
      // renameSync overwrite on Windows — the old "remove first" workaround
      // is unnecessary and creates a non-atomic write window).
      const tmp = this.#sessionPath(session.id) + ".tmp";
      const final = this.#sessionPath(session.id);
      writeFileSync(tmp, JSON.stringify(session, null, 2), "utf-8");
      renameSync(tmp, final);
      this.#saveIndex();
    } catch (e) {
      console.error("[session-store] Write failed:", e);
    }
  }

  #saveIndex(): void {
    try {
      const entries = [...this.#index.values()];
      writeFileSync(this.#indexFile, JSON.stringify(entries, null, 2), "utf-8");
    } catch {
      // best-effort
    }
  }

  #loadIndex(): void {
    try {
      if (existsSync(this.#indexFile)) {
        const entries = JSON.parse(readFileSync(this.#indexFile, "utf-8")) as SessionIndexEntry[];
        for (const entry of entries) {
          this.#index.set(entry.id, entry);
        }
      }

      // Also scan directory for orphaned session files
      if (existsSync(this.#dir)) {
        for (const file of readdirSync(this.#dir)) {
          if (!file.endsWith(".json") || file === "_index.json") continue;
          const id = basename(file, ".json");
          if (!this.#index.has(id)) {
            try {
              const path = join(this.#dir, file);
              const session = JSON.parse(readFileSync(path, "utf-8")) as Session;
              this.#index.set(id, {
                id,
                created: session.created,
                lastActive: session.lastActive,
                messageCount: session.messageCount,
                model: session.model,
                skill: session.skill,
              });
            } catch {
              // corrupted — skip
            }
          }
        }
      }
    } catch {
      // fresh start
    }
  }
}
