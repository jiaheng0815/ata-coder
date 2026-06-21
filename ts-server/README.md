# ATA Coder TypeScript Companion Server

Node.js 24 native TypeScript HTTP API server — a high-performance companion
to the Python ATA Coder core.

## Architecture

```
┌────────────────────────────────────────────────────┐
│  TypeScript Server (Node.js 24)                    │
│                                                    │
│  HTTP / SSE  ←→  Shell Sessions  ←→  MCP Bridge    │
│       │               │                 │          │
│       │        node-pty PTY        JSON-RPC        │
│       │               │            (stdio/HTTP)    │
│       ▼               ▼                 ▼          │
│  ┌─────────────────────────────────────────────┐   │
│  │  Python ATA Coder Core (subprocess IPC)     │   │
│  │  agent.py · tools · LLM clients · skills    │   │
│  └─────────────────────────────────────────────┘   │
└────────────────────────────────────────────────────┘
```

## Node.js 24 Features Used

| Feature | Where | Benefit |
|---------|-------|---------|
| **Native TypeScript** | All `.ts` files | Run directly, no build step |
| **`using` keyword** | AgentBridge, ShellManager, McpBridge | Deterministic resource cleanup |
| **`AsyncLocalStorage`** | Request tracing | Per-request context without passing params |
| **V8 13.6 JSON** | All serialization | ~2× faster JSON.stringify |
| **Native `fetch`** (Undici 7) | MCP HTTP transport | HTTP/2 support, zero deps |
| **`node:test`** | `server.test.ts` | Built-in test runner, no Jest needed |
| **`require(esm)`** | Module loading | Seamless CJS↔ESM interop |

## Quick Start

```bash
# Requires Node.js >= 24.0.0
node --version  # should be v24.x

# Install dependencies (only node-pty for shell sessions)
npm install

# Run (native TS — no build step!)
npm start -- --port 8080 --python python --workspace /path/to/project

# Development with watch mode
npm run dev

# Run tests
npm test

# Type-check only (optional, for CI)
npm run type-check
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check + server stats |
| POST | `/v1/chat` | Agent chat (SSE streaming or JSON) |
| GET | `/v1/sessions` | List active sessions |
| DELETE | `/v1/sessions/:id` | Delete a session |
| POST | `/v1/shell/open` | Open persistent shell |
| POST | `/v1/shell/exec` | Execute command in shell |
| POST | `/v1/shell/close` | Close shell session |
| GET | `/v1/mcp/tools` | List MCP tools |

## Compatibility

The Python `ata` CLI and `pip install ata_coder` remain the primary distribution
method. This TypeScript server is an **optional companion** — it does not
replace or modify the Python core. It communicates with Python via subprocess
JSON-RPC, preserving full compatibility with the existing codebase.

## Performance

- **I/O-bound workloads** (HTTP serving, SSE streaming, shell I/O): Node.js
  event loop handles concurrency efficiently
- **CPU-bound workloads** (LLM inference, tool execution): Delegated to Python
  subprocess — no performance regression
- **JSON serialization**: V8 13.6 provides ~2× speedup on large tool results
- **Startup**: <100ms (vs ~500ms for Python server import chain)
