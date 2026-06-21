/**
 * Shared types for the ATA Coder TypeScript companion server.
 *
 * Uses Node.js 24 native TypeScript — run directly with:
 *   node --experimental-transform-types src/server.ts
 *
 * Node.js 24 features leveraged:
 *  - Native TS (no build step)
 *  - `using` keyword for SyncDisposable resource cleanup
 *  - V8 13.6 JSON optimizations
 *  - `AsyncLocalStorage` for request context
 */

// ── IPC Protocol (Python ↔ TypeScript) ──────────────────────────────────────

export interface AgentRequest {
  /** Unique request ID for correlation */
  id: string;
  /** Operation type */
  op: "run" | "cancel" | "status" | "shutdown";
  /** Agent task text (for 'run' op) */
  task?: string;
  /** Skill name override */
  skill?: string;
  /** Explicit model override */
  model?: string;
  /** Stream mode */
  stream?: boolean;
  /** Session ID for persistent sessions */
  sessionId?: string;
  /** Whether to reset context */
  resetContext?: boolean;
}

export interface AgentResponse {
  id: string;
  status: "ok" | "error" | "stream" | "done";
  /** Final response text (when status is 'done') */
  text?: string;
  /** Error message */
  error?: string;
  /** Stream event type */
  event?: StreamEvent;
}

// ── SSE / Streaming Events ──────────────────────────────────────────────────

export type StreamEvent =
  | TextDeltaEvent
  | ToolCallEvent
  | ToolResultEvent
  | ToolStreamEvent
  | ThinkingEvent
  | ErrorEvent
  | CompleteEvent;

export interface TextDeltaEvent {
  type: "text_delta";
  content: string;
}

export interface ToolCallEvent {
  type: "tool_call";
  tool_name: string;
  arguments: Record<string, unknown>;
}

export interface ToolResultEvent {
  type: "tool_result";
  tool_name: string;
  success: boolean;
  output: string;
  error?: string;
}

export interface ToolStreamEvent {
  type: "tool_stream";
  tool_name: string;
  chunk: string;
}

export interface ThinkingEvent {
  type: "thinking";
  content: string;
}

export interface ErrorEvent {
  type: "error";
  message: string;
}

export interface CompleteEvent {
  type: "complete";
  text: string;
  /** Token usage stats */
  usage?: TokenUsage;
}

// ── Token & Usage ───────────────────────────────────────────────────────────

export interface TokenUsage {
  promptTokens: number;
  completionTokens: number;
  totalTokens: number;
}

// ── Session Management ──────────────────────────────────────────────────────

export interface SessionInfo {
  id: string;
  created: string; // ISO 8601
  lastActive: string;
  messageCount: number;
  model: string;
  skill?: string;
}

// ── Shell Session ──────────────────────────────────────────────────────────

export interface ShellSession {
  id: string;
  cwd: string;
  shell: string; // "powershell" | "bash" | "cmd"
  createdAt: number;
  lastUsed: number;
}

// ── MCP Server Configuration ────────────────────────────────────────────────

export interface McpServerConfig {
  name: string;
  transport: "stdio" | "http";
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  cwd?: string;
  url?: string;
  headers?: Record<string, string>;
}

// ── Server Configuration ────────────────────────────────────────────────────

export interface ServerConfig {
  port: number;
  host: string;
  pythonPath: string;   // path to Python interpreter
  workspaceDir: string;
  maxConcurrentAgents: number;
  sessionTtlSeconds: number;
  shellTtlSeconds: number;
  maxThreads: number;
  mcpServers: McpServerConfig[];
}
