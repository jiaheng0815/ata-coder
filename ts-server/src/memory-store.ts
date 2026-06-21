/**
 * Memory Store — replaces memory.py.
 *
 * Persistent file-based memory with TF-IDF search.
 * Each memory is one .md file with YAML frontmatter in ~/.ata_coder/memory/.
 *
 * Node.js 24: single-threaded event loop — no locks needed (unlike Python).
 */

import {
  readFileSync, writeFileSync, existsSync, mkdirSync,
  readdirSync, unlinkSync, statSync,
} from "node:fs";
import { join, dirname } from "node:path";
import { ATA_HOME } from "./config.ts";

// ── Types ───────────────────────────────────────────────────────────────────

export interface Memory {
  name: string;
  description: string;
  metadata: {
    type: "user" | "feedback" | "project" | "reference";
  };
  content: string;
  filePath: string;
  updated: string;
  memoryType: string;
  /** Raw frontmatter body (excluding YAML) */
  body: string;
}

// ── Constants ───────────────────────────────────────────────────────────────

const MEMORY_DIR = join(ATA_HOME, "memory");
const INDEX_FILE = join(MEMORY_DIR, "MEMORY.md");

const STOPWORDS = new Set([
  "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
  "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
  "being", "have", "has", "had", "do", "does", "did", "will", "would",
  "could", "should", "may", "might", "can", "shall", "this", "that",
  "these", "those", "i", "me", "my", "we", "our", "you", "your", "he",
  "she", "it", "they", "them", "not", "no", "nor", "so", "if", "then",
  "than", "too", "very", "just", "about", "also", "as", "into", "like",
  "up", "out", "when", "where", "how", "all", "both", "each", "every",
  "more", "most", "other", "some", "such", "only", "own", "same",
]);

// ── YAML frontmatter parser (zero-dependency) ───────────────────────────────

function parseFrontmatter(raw: string): { data: Record<string, string>; body: string } | null {
  const match = raw.match(/^---\n([\s\S]*?)\n---\n([\s\S]*)$/);
  if (!match) return null;

  const data: Record<string, string> = {};
  for (const line of match[1].split("\n")) {
    const kv = line.match(/^(\w[\w-]*):\s*(.*)$/);
    if (kv) data[kv[1]] = kv[2].trim();
  }
  return { data, body: match[2].trim() };
}

// ── Tokenizer ───────────────────────────────────────────────────────────────

function tokenize(text: string): string[] {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9\s-]/g, " ")
    .split(/\s+/)
    .filter((t) => t.length > 1 && !STOPWORDS.has(t));
}

// ── Memory Store ────────────────────────────────────────────────────────────

export class MemoryStore implements Disposable {
  readonly #memories = new Map<string, Memory>();
  #allLoaded = false;

  constructor() {
    if (!existsSync(MEMORY_DIR)) mkdirSync(MEMORY_DIR, { recursive: true });
  }

  [Symbol.dispose](): void {
    this.#rebuildIndex();
  }

  // ── CRUD ──────────────────────────────────────────────────────────────────

  add(name: string, description: string, content: string, type: Memory["metadata"]["type"]): Memory {
    this.#ensureLoaded();

    const now = new Date().toISOString();
    const filePath = `${name}.md`;
    const frontmatter = [
      "---",
      `name: ${name}`,
      `description: ${description}`,
      "metadata:",
      `  type: ${type}`,
      "---",
      "",
      content,
    ].join("\n");

    const fullPath = join(MEMORY_DIR, filePath);
    writeFileSync(fullPath, frontmatter, "utf-8");

    const memory: Memory = {
      name,
      description,
      metadata: { type },
      content,
      filePath,
      updated: now,
      memoryType: type,
      body: content,
    };

    this.#memories.set(name, memory);
    return memory;
  }

  get(name: string): Memory | null {
    this.#ensureLoaded();
    return this.#memories.get(name) ?? null;
  }

  delete(name: string): boolean {
    const memory = this.#memories.get(name);
    if (!memory) return false;
    this.#memories.delete(name);
    try {
      const fullPath = join(MEMORY_DIR, memory.filePath);
      if (existsSync(fullPath)) unlinkSync(fullPath);
    } catch {
      // best-effort
    }
    return true;
  }

  list(type?: string): Memory[] {
    this.#ensureLoaded();
    const memories = [...this.#memories.values()];
    const filtered = type ? memories.filter((m) => m.memoryType === type) : memories;
    return filtered.sort((a, b) => b.updated.localeCompare(a.updated));
  }

  search(query: string): Memory[] {
    this.#ensureLoaded();
    return this.#searchScored(query);
  }

  // ── TF-IDF Search ─────────────────────────────────────────────────────────

  #searchScored(query: string): Memory[] {
    const queryTokens = tokenize(query);
    if (queryTokens.length === 0) return [];

    const memories = [...this.#memories.values()];
    const docCount = memories.length;
    if (docCount === 0) return [];

    // Compute IDF
    const df = new Map<string, number>();
    for (const m of memories) {
      const text = `${m.name} ${m.description} ${m.body}`;
      const tokens = new Set(tokenize(text));
      for (const t of tokens) {
        df.set(t, (df.get(t) ?? 0) + 1);
      }
    }

    const idf = new Map<string, number>();
    for (const [token, count] of df) {
      idf.set(token, Math.log((docCount + 1) / (count + 1)) + 1);
    }

    // Score each document
    const scored: Array<[number, Memory]> = [];
    for (const m of memories) {
      const text = `${m.name} ${m.description} ${m.body}`;
      const docTokens = tokenize(text);
      const tf = new Map<string, number>();
      for (const t of docTokens) {
        tf.set(t, (tf.get(t) ?? 0) + 1);
      }

      let score = 0;
      for (const qt of queryTokens) {
        const idfVal = idf.get(qt) ?? 0;
        const tfVal = tf.get(qt) ?? 0;
        score += tfVal * idfVal;
      }
      // Bonus for name match
      if (m.name.toLowerCase().includes(query.toLowerCase())) score += 10;

      if (score > 0) scored.push([score, m]);
    }

    scored.sort((a, b) => b[0] - a[0]);
    return scored.map(([, m]) => m);
  }

  // ── Index ─────────────────────────────────────────────────────────────────

  #rebuildIndex(): void {
    try {
      const lines: string[] = ["# Memory Index", ""];
      for (const [, m] of this.#memories) {
        lines.push(`- [${m.name}](${m.filePath}) — ${m.description}`);
      }
      writeFileSync(INDEX_FILE, lines.join("\n") + "\n", "utf-8");
    } catch {
      // best-effort
    }
  }

  #ensureLoaded(): void {
    if (this.#allLoaded) return;
    try {
      if (!existsSync(MEMORY_DIR)) return;
      for (const file of readdirSync(MEMORY_DIR)) {
        if (!file.endsWith(".md") || file === "MEMORY.md") continue;
        const full = join(MEMORY_DIR, file);
        try {
          const raw = readFileSync(full, "utf-8");
          const parsed = parseFrontmatter(raw);
          if (!parsed) continue;
          const stat = statSync(full);
          const memory: Memory = {
            name: parsed.data.name ?? file.replace(".md", ""),
            description: parsed.data.description ?? "",
            metadata: {
              type: (parsed.data["metadata.type"] ?? "reference") as Memory["metadata"]["type"],
            },
            content: raw,
            filePath: file,
            updated: stat.mtime.toISOString(),
            memoryType: parsed.data["metadata.type"] ?? "reference",
            body: parsed.body,
          };
          this.#memories.set(memory.name, memory);
        } catch {
          // skip corrupted files
        }
      }
    } catch {
      // directory doesn't exist yet
    }
    this.#allLoaded = true;
  }
}
