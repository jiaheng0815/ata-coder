/**
 * ATA Coder CLI — TypeScript entry point (replaces main.py).
 *
 * Run with Node.js 24 native TypeScript:
 *   node --experimental-transform-types src/cli.ts [command] [options]
 *
 * Commands:
 *   (none)     Interactive REPL mode
 *   run <task> Single-task mode (non-interactive)
 *   server     HTTP API server mode
 *   init       First-run setup wizard
 */

import { resolve } from "node:path";
import { createConfig, writeDefaultSettings, AppConfig } from "./config.ts";
import { AgentBridge } from "./agent-bridge.ts";
import { AtaCoderServer } from "./server.ts";
import { SessionStore } from "./session-store.ts";
import { MemoryStore } from "./memory-store.ts";
import { ChangeTracker } from "./change-tracker.ts";
import { PermissionStore, PermissionMode } from "./permissions.ts";
import { GitWorkflow } from "./git-workflow.ts";
import { detectProject } from "./project.ts";
import { dispatch } from "./commands/core.ts";

// ═══════════════════════════════════════════════════════════════════════════════
// Version banner
// ═══════════════════════════════════════════════════════════════════════════════

const VERSION = "2.5.3";

function printBanner(config: AppConfig): void {
  console.log(`
┌─ ATA Coder v${VERSION} ──────────────────────┐
│                                                        │
│  Model:  ${config.llm.model.padEnd(44)}│
│  Runtime: Node.js ${process.version.padEnd(30)} (TypeScript Native) │
│  Workspace: ${(config.agent.workspaceDir.slice(-40)).padEnd(38)}│
│                                                        │
│  Type /help for commands, /exit to quit.              │
│                                                        │
└────────────────────────────────────────────────────────┘
`);
}

// ═══════════════════════════════════════════════════════════════════════════════
// First-run wizard
// ═══════════════════════════════════════════════════════════════════════════════

async function firstRunWizard(force = false): Promise<void> {
  const readline = await import("node:readline");
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  const ask = (q: string): Promise<string> => new Promise((r) => rl.question(q, r));

  console.log("\n  Welcome to ATA Coder! First-run setup.\n");

  const apiKey = await ask("  API Key: ");
  const baseUrl = await ask("  Base URL [https://api.deepseek.com]: ");
  const model = await ask("  Default Model [deepseek-v4-pro]: ");

  writeDefaultSettings(apiKey || "", baseUrl || undefined);
  console.log(`\n  ✅ Configuration saved to ~/.ata_coder/settings.json\n`);

  if (model) {
    const config = createConfig();
    config.llm.model = model;
  }

  rl.close();
}

// ═══════════════════════════════════════════════════════════════════════════════
// Interactive REPL
// ═══════════════════════════════════════════════════════════════════════════════

async function runInteractive(config: AppConfig): Promise<void> {
  printBanner(config);

  // Prefer ATA_CODER_PYTHON env var; fall back to "python3" on non-Windows, "python" otherwise.
  const pythonPath = process.env.ATA_CODER_PYTHON
    ?? (process.platform === "win32" ? "python" : "python3");
  using bridge = new AgentBridge(pythonPath, config.agent.workspaceDir);
  using sessions = new SessionStore();
  using memory = new MemoryStore();
  using changes = new ChangeTracker();
  using perms = new PermissionStore();

  const project = detectProject(config.agent.workspaceDir);
  let currentSession = sessions.create(config.llm.model);

  console.log(`  Project: ${project.language}${project.framework ? ` (${project.framework})` : ""}`);
  if (project.hasGit) console.log(`  Git: detected`);

  const readline = await import("node:readline");
  const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
    prompt: "\x1b[36mata>\x1b[0m ",
  });

  let running = true;

  const ctx = {
    config,
    sessions,
    currentSession,
    print: (text: string) => console.log(text),
    exit: () => { running = false; rl.close(); },
    clearContext: () => {
      currentSession = sessions.create(config.llm.model);
      console.log("  Context cleared.");
    },
  };

  rl.prompt();

  for await (const line of rl) {
    const trimmed = line.trim();
    if (!trimmed) {
      rl.prompt();
      continue;
    }

    // Check if it's a slash command
    if (dispatch(trimmed, ctx)) {
      if (!running) break;
      rl.prompt();
      continue;
    }

    // Execute via Python agent bridge
    console.log(""); // blank line before response
    try {
      const startTime = Date.now();
      const result = await bridge.runTask(trimmed, {
        sessionId: currentSession.id,
      });

      if (result.text) {
        console.log(result.text);
        console.log(`\n  ⏱ ${((Date.now() - startTime) / 1000).toFixed(1)}s`);
      } else if (result.error) {
        console.log(`  ❌ Error: ${result.error}`);
      }
    } catch (e) {
      console.log(`  ❌ Failed: ${e}`);
    }

    // Update session
    currentSession.messageCount++;
    sessions.save(currentSession);

    console.log(""); // blank line before next prompt
    rl.prompt();
  }

  rl.close();
  console.log("\n  Bye! ❤️\n");
}

// ═══════════════════════════════════════════════════════════════════════════════
// Single-task mode
// ═══════════════════════════════════════════════════════════════════════════════

async function runTask(config: AppConfig, task: string): Promise<void> {
  const pythonPath = process.env.ATA_CODER_PYTHON
    ?? (process.platform === "win32" ? "python" : "python3");
  using bridge = new AgentBridge(pythonPath, config.agent.workspaceDir);

  console.log(`  Task: ${task.slice(0, 80)}${task.length > 80 ? "…" : ""}`);
  console.log(`  Model: ${config.llm.model}\n`);

  try {
    const startTime = Date.now();
    const result = await bridge.runTask(task);

    if (result.text) {
      console.log(result.text);
      console.log(`\n  ⏱ ${((Date.now() - startTime) / 1000).toFixed(1)}s`);
    } else if (result.error) {
      console.error(`  Error: ${result.error}`);
      process.exit(1);
    }
  } catch (e) {
    console.error(`  Fatal: ${e}`);
    process.exit(1);
  }
}

// ═══════════════════════════════════════════════════════════════════════════════
// Arg parsing (zero-dependency)
// ═══════════════════════════════════════════════════════════════════════════════

function parseCliArgs(): {
  command: string;
  task: string;
  options: Record<string, string | boolean>;
} {
  const args = process.argv.slice(2);
  const options: Record<string, string | boolean> = {};
  const positional: string[] = [];

  for (let i = 0; i < args.length; i++) {
    if (args[i].startsWith("--")) {
      const eqIdx = args[i].indexOf("=");
      if (eqIdx > 0) {
        const key = args[i].slice(2, eqIdx);
        options[key] = args[i].slice(eqIdx + 1);
      } else {
        const key = args[i].slice(2);
        const next = args[i + 1];
        if (next && !next.startsWith("--")) {
          options[key] = next;
          i++;
        } else {
          options[key] = true;
        }
      }
    } else {
      positional.push(args[i]);
    }
  }

  return {
    command: positional[0] ?? "",
    task: positional.slice(1).join(" "),
    options,
  };
}

// ═══════════════════════════════════════════════════════════════════════════════
// Main
// ═══════════════════════════════════════════════════════════════════════════════

async function main(): Promise<void> {
  const { command, task, options } = parseCliArgs();

  // Version flag
  if (options.version) {
    console.log(`ATA Coder v${VERSION}`);
    console.log(`Node.js ${process.version}`);
    return;
  }

  // First-run check
  if (command === "init") {
    await firstRunWizard(true);
    return;
  }

  const config = createConfig({
    model: typeof options.model === "string" ? options.model : undefined,
    baseUrl: typeof options["base-url"] === "string" ? options["base-url"] : undefined,
    apiKey: typeof options["api-key"] === "string" ? options["api-key"] : undefined,
    workspaceDir: typeof options.workspace === "string" ? options.workspace : undefined,
    useAnthropic: options.anthropic === true,
    verbose: options.verbose === true,
  });

  // Check if API key is configured
  if (!config.llm.apiKey && command !== "server") {
    console.log("  No API key found. Running first-run setup…");
    await firstRunWizard();
    // Re-read config after setup
    const newConfig = createConfig({
      model: typeof options.model === "string" ? options.model : undefined,
      baseUrl: typeof options["base-url"] === "string" ? options["base-url"] : undefined,
      apiKey: typeof options["api-key"] === "string" ? options["api-key"] : undefined,
      workspaceDir: typeof options.workspace === "string" ? options.workspace : undefined,
    });
    if (!newConfig.llm.apiKey) {
      console.error("  API key is required. Run `ata init` to configure.");
      process.exit(1);
    }
  }

  switch (command) {
    case "run":
      if (!task) {
        console.error("  Usage: ata run <task>");
        process.exit(1);
      }
      await runTask(config, task);
      break;

    case "server": {
      const port = typeof options.port === "string" ? parseInt(options.port, 10) : 8080;
      using server = new AtaCoderServer({
        port,
        host: typeof options.host === "string" ? options.host : "127.0.0.1",
        pythonPath: typeof options.python === "string" ? options.python : "python",
        workspaceDir: config.agent.workspaceDir,
        maxConcurrentAgents: config.agent.maxSubAgents,
        sessionTtlSeconds: 3600,
        shellTtlSeconds: 3600,
        maxThreads: 50,
        mcpServers: [],
      });
      await server.start();
      break;
    }

    case "":
      // Interactive REPL
      await runInteractive(config);
      break;

    default:
      console.error(`  Unknown command: ${command}`);
      console.error("  Usage: ata [run <task> | server | init]");
      process.exit(1);
  }
}

// Run if executed directly
if (process.argv[1]?.includes("cli")) {
  main().catch((e) => {
    console.error("Fatal:", e);
    process.exit(1);
  });
}

export { main };
