/**
 * Permissions store — replaces permissions.py.
 *
 * Interactive allow/deny/ask per tool category (read/write/shell/mcp).
 * Persisted to ~/.ata_coder/permissions.json.
 */

import { readFileSync, writeFileSync, existsSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { PERMISSIONS_FILE } from "./config.ts";

// ── Types ───────────────────────────────────────────────────────────────────

export const enum PermissionMode {
  ALLOW = "allow",
  DENY = "deny",
  ASK = "ask",
}

export type ToolCategory = "read" | "write" | "shell" | "mcp" | "subagent";

interface PermissionsFile {
  defaults?: Partial<Record<ToolCategory, PermissionMode>>;
  rules?: Array<{
    category: ToolCategory;
    mode: PermissionMode;
    pattern?: string;
    tool?: string;
    description?: string;
  }>;
}

// ── Tool → Category mapping ─────────────────────────────────────────────────

const CATEGORY_MAP: Record<string, ToolCategory> = {
  read_file: "read",
  grep: "read",
  glob: "read",
  list_dir: "read",
  write_file: "write",
  edit_file: "write",
  rename_symbol: "write",
  run_shell: "shell",
  web_search: "read",
  web_fetch: "read",
  analyze_image: "read",
  spawn_subagent: "subagent",
  collect_subagent: "subagent",
  list_subagents: "subagent",
};

export function toolCategory(toolName: string): ToolCategory {
  return CATEGORY_MAP[toolName] ?? (toolName.startsWith("mcp__") ? "mcp" : "read");
}

// ── Permissions Store ───────────────────────────────────────────────────────

export class PermissionStore implements Disposable {
  readonly #defaults: Record<ToolCategory, PermissionMode> = {
    read: PermissionMode.ALLOW,
    write: PermissionMode.ASK,
    shell: PermissionMode.ASK,
    mcp: PermissionMode.ASK,
    subagent: PermissionMode.ASK,
  };
  readonly #rules: Array<{
    category: ToolCategory;
    mode: PermissionMode;
    pattern?: RegExp;
    tool?: string;
  }> = [];

  constructor() {
    this.#load();
  }

  [Symbol.dispose](): void {
    this.#save();
  }

  // ── Query ─────────────────────────────────────────────────────────────────

  getMode(toolName: string): PermissionMode {
    const cat = toolCategory(toolName);

    // Check exact rules first
    for (const rule of this.#rules) {
      if (rule.tool === toolName) return rule.mode;
      if (rule.pattern?.test(toolName)) return rule.mode;
    }

    // Fall back to category default
    for (const rule of this.#rules) {
      if (rule.category === cat && !rule.pattern && !rule.tool) return rule.mode;
    }

    return this.#defaults[cat];
  }

  allow(toolName: string): void {
    this.#rules.push({ category: toolCategory(toolName), mode: PermissionMode.ALLOW, tool: toolName });
  }

  deny(toolName: string): void {
    this.#rules.push({ category: toolCategory(toolName), mode: PermissionMode.DENY, tool: toolName });
  }

  setDefault(category: ToolCategory, mode: PermissionMode): void {
    this.#defaults[category] = mode;
  }

  reset(): void {
    this.#rules.length = 0;
    this.#defaults.read = PermissionMode.ALLOW;
    this.#defaults.write = PermissionMode.ASK;
    this.#defaults.shell = PermissionMode.ASK;
    this.#defaults.mcp = PermissionMode.ASK;
    this.#defaults.subagent = PermissionMode.ASK;
  }

  // ── Persistence ───────────────────────────────────────────────────────────

  #save(): void {
    try {
      const dir = dirname(PERMISSIONS_FILE);
      if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
      const data: PermissionsFile = {
        defaults: { ...this.#defaults },
        rules: this.#rules.map((r) => ({
          category: r.category,
          mode: r.mode,
          tool: r.tool,
          pattern: r.pattern?.source,
        })),
      };
      writeFileSync(PERMISSIONS_FILE, JSON.stringify(data, null, 2), "utf-8");
    } catch {
      // best-effort
    }
  }

  #load(): void {
    try {
      if (!existsSync(PERMISSIONS_FILE)) return;
      const data = JSON.parse(readFileSync(PERMISSIONS_FILE, "utf-8")) as PermissionsFile;
      if (data.defaults) {
        for (const [cat, mode] of Object.entries(data.defaults)) {
          if (cat in this.#defaults) {
            this.#defaults[cat as ToolCategory] = mode;
          }
        }
      }
      if (data.rules) {
        for (const rule of data.rules) {
          this.#rules.push({
            category: rule.category,
            mode: rule.mode,
            tool: rule.tool,
            pattern: rule.pattern ? new RegExp(rule.pattern, "i") : undefined,
          });
        }
      }
    } catch {
      // fresh start
    }
  }
}
