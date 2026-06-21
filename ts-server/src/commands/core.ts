/**
 * Core slash commands — replaces commands/_core.py.
 *
 * /help, /skills, /model, /context, /version, /clear, /exit
 */

import type { AppConfig } from "../config.ts";
import type { SessionStore, Session } from "../session-store.ts";

// ── Registry ────────────────────────────────────────────────────────────────

export interface CommandContext {
  config: AppConfig;
  sessions?: SessionStore;
  currentSession?: Session;
  /** Write output to terminal */
  print: (text: string) => void;
  /** Signal to exit REPL */
  exit?: () => void;
  /** Clear the current conversation */
  clearContext?: () => void;
}

type CommandHandler = (args: string, ctx: CommandContext) => boolean;

const registry = new Map<string, { name: string; help: string; handler: CommandHandler }>();

function register(name: string, help: string, handler: CommandHandler): void {
  registry.set(name, { name, help, handler });
}

// ── Commands ────────────────────────────────────────────────────────────────

register("/help", "Show this help", (args, ctx) => {
  if (args.trim()) {
    const cmd = registry.get(args.trim());
    if (cmd) {
      ctx.print(`  ${cmd.name} — ${cmd.help}`);
      return true;
    }
    ctx.print(`  Unknown command: ${args.trim()}`);
    return true;
  }

  ctx.print("ATA Coder Commands:");
  ctx.print("");
  const groups: Record<string, Array<{ name: string; help: string }>> = {
    "Core": [],
    "Settings": [],
    "Workflow": [],
    "Safety": [],
  };
  for (const [, cmd] of registry) {
    if (["/help", "/skills", "/model", "/context", "/version", "/clear", "/exit"].includes(cmd.name)) {
      groups["Core"].push(cmd);
    } else if (["/config", "/permissions", "/mcp", "/mcp-tools"].includes(cmd.name)) {
      groups["Settings"].push(cmd);
    } else if (["/plan", "/review", "/fix", "/git"].includes(cmd.name)) {
      groups["Workflow"].push(cmd);
    } else {
      groups["Safety"].push(cmd);
    }
  }
  for (const [group, cmds] of Object.entries(groups)) {
    if (cmds.length === 0) continue;
    ctx.print(`  ${group}:`);
    for (const cmd of cmds) ctx.print(`    ${cmd.name.padEnd(18)} ${cmd.help}`);
    ctx.print("");
  }
  return true;
});

register("/skills", "List available skills", (_args, ctx) => {
  ctx.print("Skills: (auto-detection active)");
  ctx.print("  debugger    — Debug and fix code");
  ctx.print("  architect   — Design system architecture");
  ctx.print("  reviewer    — Code review");
  ctx.print("  default     — General-purpose coding assistant");
  return true;
});

register("/model", "Show or change the current model", (args, ctx) => {
  if (args.trim()) {
    ctx.print(`Model set to: ${args.trim()}`);
    // In a full implementation, this would update config.llm.model
  } else {
    ctx.print(`Current model: ${ctx.config.llm.model}`);
    ctx.print(`  Base URL: ${ctx.config.llm.baseUrl}`);
    ctx.print(`  Anthropic mode: ${ctx.config.llm.useAnthropic ? "ON" : "OFF"}`);
    ctx.print(`  Opus: ${ctx.config.llm.opusModel}`);
    ctx.print(`  Sonnet: ${ctx.config.llm.sonnetModel}`);
    ctx.print(`  Haiku: ${ctx.config.llm.haikuModel}`);
  }
  return true;
});

register("/context", "Show token/context usage", (_args, ctx) => {
  const session = ctx.currentSession;
  if (!session) {
    ctx.print("No active session.");
    return true;
  }
  ctx.print(`Session: ${session.id.slice(0, 8)}…`);
  ctx.print(`  Messages: ${session.messageCount}`);
  ctx.print(`  Tool calls: ${session.toolCallCount}`);
  ctx.print(`  Model: ${session.model}`);
  ctx.print(`  Active since: ${session.created}`);
  return true;
});

register("/version", "Show version info", (_args, ctx) => {
  ctx.print(`ATA Coder v${ctx.config.version}`);
  ctx.print(`Runtime: Node.js ${process.version} (TypeScript Native)`);
  ctx.print(`Python: ${ctx.config.llm.model}`);
  return true;
});

register("/clear", "Clear the conversation", (_args, ctx) => {
  ctx.clearContext?.();
  ctx.print("Conversation cleared. Starting fresh!");
  return true;
});

register("/exit", "Exit the REPL", (_args, ctx) => {
  ctx.exit?.();
  return true;
});

register("/quit", "Alias for /exit", (_args, ctx) => {
  ctx.exit?.();
  return true;
});

// ── Dispatch ─────────────────────────────────────────────────────────────────

export function dispatch(line: string, ctx: CommandContext): boolean {
  const trimmed = line.trim();
  if (!trimmed.startsWith("/")) return false;

  const spaceIdx = trimmed.indexOf(" ");
  const name = spaceIdx > 0 ? trimmed.slice(0, spaceIdx) : trimmed;
  const args = spaceIdx > 0 ? trimmed.slice(spaceIdx + 1) : "";

  const cmd = registry.get(name);
  if (!cmd) {
    ctx.print(`Unknown command: ${name} (type /help for available commands)`);
    return true;
  }

  try {
    return cmd.handler(args, ctx);
  } catch (e) {
    ctx.print(`Command error: ${e}`);
    return true;
  }
}

export function listCommands(): Array<{ name: string; help: string }> {
  return [...registry.values()];
}
