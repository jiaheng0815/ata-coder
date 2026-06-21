/**
 * Configuration system — replaces config.py / settings.py.
 *
 * Resolution priority (identical to Python):
 *   1. CLI args / environment variables
 *   2. settings.json `env` block
 *   3. settings.json legacy keys
 *   4. hardcoded defaults
 */

import { readFileSync, existsSync, mkdirSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { homedir } from "node:os";

// ── Paths ───────────────────────────────────────────────────────────────────

const ATA_HOME = join(homedir(), ".ata_coder");
const SETTINGS_FILE = join(ATA_HOME, "settings.json");
const PERMISSIONS_FILE = join(ATA_HOME, "permissions.json");
const SESSIONS_DIR = join(ATA_HOME, "sessions");

// ── Defaults ────────────────────────────────────────────────────────────────

const DEFAULTS = {
  model: "gpt-4o",
  opusModel: "gpt-4o",
  sonnetModel: "gpt-4o",
  haikuModel: "gpt-4o-mini",
  subagentModel: "gpt-4o-mini",
  maxOutputTokens: 16384,
  effortLevel: "medium",
  temperature: 0.1,
  baseUrl: "https://api.openai.com/v1",
  useAnthropic: false,
  maxSubAgents: 5,
  searchBackend: "bing",
  allowAll: false,
  maxContextTokens: 200_000,
  cleanupPeriodDays: 30,
};

// ── Config Types ────────────────────────────────────────────────────────────

export interface LlmConfig {
  apiKey: string;
  baseUrl: string;
  model: string;
  opusModel: string;
  sonnetModel: string;
  haikuModel: string;
  subagentModel: string;
  maxOutputTokens: number;
  effortLevel: string;
  temperature: number;
  useAnthropic: boolean;
}

export interface AgentConfig {
  workspaceDir: string;
  maxSubAgents: number;
  maxContextTokens: number;
  searchBackend: string;
  allowAll: boolean;
  cleanupPeriodDays: number;
}

export interface AppConfig {
  llm: LlmConfig;
  agent: AgentConfig;
  version: string;
  settingsFile: string;
  sessionsDir: string;
  permissionsFile: string;
}

// ── Settings reader ─────────────────────────────────────────────────────────

interface SettingsFile {
  env?: Record<string, string>;
  api?: { key?: string; base_url?: string };
  model?: { default?: string; opus?: string; sonnet?: string; haiku?: string; subagent?: string };
  complexity?: { auto_detect?: boolean; simple_max_chars?: number; complex_min_chars?: number };
  paths?: { data?: string; skills?: string };
  cleanupPeriodDays?: number;
}

function loadSettings(): SettingsFile {
  try {
    if (existsSync(SETTINGS_FILE)) {
      return JSON.parse(readFileSync(SETTINGS_FILE, "utf-8")) as SettingsFile;
    }
  } catch {
    console.warn("[config] Failed to parse settings.json — using defaults");
  }
  return {};
}

function ensureDirs(): void {
  if (!existsSync(ATA_HOME)) mkdirSync(ATA_HOME, { recursive: true });
  if (!existsSync(SESSIONS_DIR)) mkdirSync(SESSIONS_DIR, { recursive: true });
}

/** Write initial settings.json (first-run setup) */
export function writeDefaultSettings(apiKey: string, baseUrl?: string): void {
  ensureDirs();
  const settings: SettingsFile = {
    env: {
      ATA_CODER_API_KEY: apiKey,
      ATA_CODER_BASE_URL: baseUrl ?? DEFAULTS.baseUrl,
      ATA_CODER_DEFAULT_MODEL: DEFAULTS.model,
      ATA_CODER_DEFAULT_OPUS_MODEL: DEFAULTS.opusModel,
      ATA_CODER_DEFAULT_SONNET_MODEL: DEFAULTS.sonnetModel,
      ATA_CODER_DEFAULT_HAIKU_MODEL: DEFAULTS.haikuModel,
      ATA_CODER_SUBAGENT_MODEL: DEFAULTS.subagentModel,
      ATA_CODER_MAX_OUTPUT_TOKENS: String(DEFAULTS.maxOutputTokens),
      ATA_CODER_EFFORT_LEVEL: DEFAULTS.effortLevel,
    },
    complexity: { auto_detect: true, simple_max_chars: 60, complex_min_chars: 500 },
    cleanupPeriodDays: DEFAULTS.cleanupPeriodDays,
  };
  try {
    writeFileSync(SETTINGS_FILE, JSON.stringify(settings, null, 2), "utf-8");
  } catch {
    console.error("[config] Failed to write settings.json");
  }
}

// ── Config factory ──────────────────────────────────────────────────────────

export function createConfig(overrides: Partial<{
  model: string; baseUrl: string; apiKey: string;
  workspaceDir: string; useAnthropic: boolean; verbose: boolean;
}> = {}): AppConfig {
  ensureDirs();
  const settings = loadSettings();
  const env = { ...process.env, ...settings.env };

  const llm: LlmConfig = {
    apiKey: overrides.apiKey
      ?? env.ATA_CODER_API_KEY
      ?? env.OPENAI_API_KEY
      ?? settings.api?.key
      ?? "",
    baseUrl: overrides.baseUrl
      ?? env.ATA_CODER_BASE_URL
      ?? env.OPENAI_BASE_URL
      ?? settings.api?.base_url
      ?? DEFAULTS.baseUrl,
    model: overrides.model
      ?? env.ATA_CODER_DEFAULT_MODEL
      ?? env.OPENAI_MODEL
      ?? settings.model?.default
      ?? DEFAULTS.model,
    opusModel: env.ATA_CODER_DEFAULT_OPUS_MODEL ?? settings.model?.opus ?? DEFAULTS.opusModel,
    sonnetModel: env.ATA_CODER_DEFAULT_SONNET_MODEL ?? settings.model?.sonnet ?? DEFAULTS.sonnetModel,
    haikuModel: env.ATA_CODER_DEFAULT_HAIKU_MODEL ?? settings.model?.haiku ?? DEFAULTS.haikuModel,
    subagentModel: env.ATA_CODER_SUBAGENT_MODEL ?? settings.model?.subagent ?? DEFAULTS.subagentModel,
    maxOutputTokens: parseInt(env.ATA_CODER_MAX_OUTPUT_TOKENS ?? String(DEFAULTS.maxOutputTokens), 10),
    effortLevel: env.ATA_CODER_EFFORT_LEVEL ?? env.THINKING_STRENGTH ?? DEFAULTS.effortLevel,
    temperature: parseFloat(env.TEMPERATURE ?? String(DEFAULTS.temperature)),
    useAnthropic: overrides.useAnthropic ?? (env.ATA_CODER_USE_ANTHROPIC === "1"),
  };

  // Strip DeepSeek [1m] suffix
  for (const key of ["model", "opusModel", "sonnetModel", "haikuModel", "subagentModel"] as const) {
    llm[key] = llm[key].replace(/\[1m\]$/, "");
  }

  const agent: AgentConfig = {
    workspaceDir: resolve(overrides.workspaceDir ?? env.WORKSPACE_DIR ?? process.cwd()),
    maxSubAgents: parseInt(env.MAX_SUB_AGENTS ?? String(DEFAULTS.maxSubAgents), 10),
    maxContextTokens: DEFAULTS.maxContextTokens,
    searchBackend: env.ATA_CODER_SEARCH_BACKEND ?? DEFAULTS.searchBackend,
    allowAll: env.ATA_CODER_ALLOW_ALL === "1",
    cleanupPeriodDays: settings.cleanupPeriodDays ?? DEFAULTS.cleanupPeriodDays,
  };

  return {
    llm,
    agent,
    version: "2.5.3",
    settingsFile: SETTINGS_FILE,
    sessionsDir: SESSIONS_DIR,
    permissionsFile: PERMISSIONS_FILE,
  };
}

// ── Helpers ─────────────────────────────────────────────────────────────────

export { ATA_HOME, SETTINGS_FILE, PERMISSIONS_FILE, SESSIONS_DIR };
