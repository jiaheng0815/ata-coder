# ATA Coder

<p align="center">
  <strong>CLI AI Coding Assistant</strong> — OpenAI & Anthropic APIs, async streaming, built-in HTTP server.
  <br>
  Python 3.10+ · MIT License · <a href="#contributing">Contributions Welcome</a>
</p>

<p align="center">
  <a href="https://pypi.org/project/ata-coder/"><img src="https://img.shields.io/pypi/v/ata-coder?color=blue" alt="PyPI"></a>
  <a href="https://pypi.org/project/ata-coder/"><img src="https://img.shields.io/pypi/pyversions/ata-coder" alt="Python"></a>
  <a href="LICENSE"><img src="https://img.shields.io/pypi/l/ata-coder" alt="License"></a>
  <a href="https://github.com/jiaheng0815/ata-coder"><img src="https://img.shields.io/github/stars/jiaheng0815/ata-coder?style=social" alt="Stars"></a>
</p>

---

## What is ATA Coder?

ATA Coder is a terminal-native AI coding assistant. Describe what you want in plain language — the AI reads your code, proposes edits, runs shell commands, and searches the web. Every destructive action asks for confirmation.

Think of it as a senior engineer sitting next to you — one who reads the whole codebase, never gets tired, and tells you *before* running `rm -rf`.

```bash
pip install ata-coder
```

Requires Python 3.10+. That's the only runtime dependency.

## Quick Start

```bash
ata                              # Interactive REPL
ata run "Fix the timeout bug"    # Single-shot task
ata server --port 8080           # HTTP API + SSE streaming
ata --skill debugger             # Activate a specific skill
ata --resume <session-id>        # Resume a saved session
```

First run walks you through API key and model setup. Config is saved to `~/.ata_coder/settings.json`.

### What a session looks like

```text
$ ata

ata v1.0.0 — gpt-4o
Type /help for commands, Ctrl+C to interrupt.

▸ This function's timeout doesn't work for streaming responses. Fix it.

```python
async def fetch(url, timeout=5):
    async with httpx.AsyncClient() as c:
        return await c.get(url, timeout=timeout)
```

[AI reads the file, identifies httpx.Timeout has separate connect/read/write
 phases, proposes a fix →]

--- a/api.py
+++ b/api.py
 async def fetch(url, timeout=5):
+    t = httpx.Timeout(connect=timeout, read=timeout, write=timeout, pool=timeout)
     async with httpx.AsyncClient() as c:
-        return await c.get(url, timeout=timeout)
+        return await c.get(url, timeout=t)

  ⚠ Modify file: api.py
  Allow? [y/N] y
  ✓ Edited api.py
```

## Features

### Core Engine

| Feature | Description |
|---------|-------------|
| **Async agent** | Single-threaded asyncio event loop — no threads, no race conditions |
| **Streaming** | Real-time LLM output + live tool output streaming for shell/search/fetch |
| **Multi-provider** | OpenAI-compatible + Anthropic Messages API via unified client interface |
| **Smart routing** | Keyword + length task classification — routes simple queries to fast models, complex tasks to capable ones, with zero extra API calls |
| **Context compaction** | O(1) token tracking with LLM summarization and force-truncate fallback |
| **Vision** | `analyze_image` tool — auto-resolves vision model/api-key/base-url with full fallback chain |

### Developer Tools (14)

| Tool | Description |
|------|-------------|
| `read_file` / `write_file` / `edit_file` | File I/O with colorized unified-diff preview before every write |
| `run_shell` | Timeout-protected, output capped at 500KB, real-time streaming |
| `grep` / `glob` | Regex content search and file pattern matching — dedicated tools, not shell hacks |
| `web_search` / `web_fetch` | DuckDuckGo search + URL text extraction (no API key needed) |
| `spawn_subagent` / `collect_subagent` | Parallel isolated sub-agents for complex multi-step tasks |
| `mcp_search` | Model Context Protocol — stdio + HTTP/SSE transport |
| `analyze_image` | Vision analysis with automatic config fallback |
| `rename_symbol` | AST-based identifier renaming (libcst) |

### Safety Pipeline

```
Pattern Guard → Fool-Proof Check → Interactive Permissions → OS Privilege Elevation
```

Every destructive action flows through all four layers before execution. Change tracking backs up every file edit — undo/redo with session-level history.

### HTTP API Server

```bash
ata server --port 8080
```

| Endpoint | Description |
|----------|-------------|
| `POST /chat` | Non-streaming chat |
| `POST /chat/stream` | SSE streaming with `tool_stream` events |
| `GET /health` | Health check |
| `GET /tools` | List available tools |
| `GET /skills` | List available skills |
| `GET /models` | List configured models |
| `POST /api/shell` | Interactive persistent shell session |
| `GET /sessions` | List active sessions |

```bash
# Streaming chat
curl -N -X POST http://localhost:8080/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"message": "Explain async/await"}'

# Non-streaming with skill
curl -X POST http://localhost:8080/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Write hello world", "skill": "general-coder"}'
```

### Skills & Extensions

Skills live in folder-based manifests (`SKILL.md`) with auto-detection and single-skill activation. Extensions plug into a pub/sub hook system with lifecycle management.

Built-in skills ship in `skills/`. Drop your own into `~/.ata_coder/skills/` — they're auto-discovered.

### TypeScript Companion (optional)

A Node.js 24 native TypeScript server lives in `ts-server/`, handling HTTP/SSE, shell management, MCP bridging, safety guarding, session persistence, git integration, and project detection. Communicates with the Python core via JSON-RPC subprocess IPC. The Python core works standalone — the TS companion adds a hardened outer layer.

## Architecture

```
asyncio Event Loop (single-threaded)
├── REPL (prompt_toolkit + Rich)
│     await controller.submit(task)
│     await event_queue.drain() → ui.on_event()
│
├── AgentController (asyncio.Task)
│     CoderAgent.run() → async LLM loop → await tool calls
│     BaseLLMClient (ABC) — unified OpenAI/Anthropic async interface
│     ExtensionManager → skill prompt aggregation
│     Keyword-based task classification (zero extra API calls)
│
├── Sub-Agent Tasks (asyncio.TaskGroup)
│     SubAgent 1..N: independent LLM, tools, isolated context
│     asyncio.Semaphore → concurrency limit
│
└── MCP Clients (asyncio subprocess)
      StdioConnection → create_subprocess_exec + async read loop
      HTTPConnection → httpx.AsyncClient
```

**No threads, no race conditions, no watchdog.** asyncio-native cancellation replaces the old thread supervisor.

## Configuration

All config lives in `~/.ata_coder/settings.json`:

```json
{
  "env": {
    "ATA_CODER_BASE_URL": "https://api.openai.com/v1",
    "ATA_CODER_API_KEY": "sk-...",
    "ATA_CODER_DEFAULT_MODEL": "gpt-4o",
    "ATA_CODER_DEFAULT_OPUS_MODEL": "gpt-4o",
    "ATA_CODER_DEFAULT_SONNET_MODEL": "gpt-4o",
    "ATA_CODER_DEFAULT_HAIKU_MODEL": "gpt-4o-mini",
    "ATA_CODER_SUBAGENT_MODEL": "gpt-4o-mini",
    "ATA_CODER_MAX_OUTPUT_TOKENS": "16384",
    "ATA_CODER_EFFORT_LEVEL": "medium"
  },
  "complexity": { "auto_detect": true },
  "paths": { "data": "~/.ata_coder" },
  "permissions": {
    "allow": ["Bash(git:*)", "Read(./**)", "Edit(./**/*.{py,json,md})"],
    "deny": ["Bash(rm -rf:*)", "Bash(sudo:*)", "Read(./.env)"]
  }
}
```

Works with any OpenAI-compatible API: **OpenAI**, **DeepSeek**, **Anthropic** (via compatible gateway), **OpenRouter**, **Ollama**, and more.

## Commands

| Command | Description |
|---------|-------------|
| `/help` | Show help |
| `/skills` | List available skills |
| `/skill <name>` | Switch active skill |
| `/review` | AI code review of git diff |
| `/fix [severity]` | Auto-fix review issues |
| `/undo [n]` | Undo file changes |
| `/changes` | List tracked changes |
| `/save` | Save current session |
| `/resume <id>` | Resume saved session |
| `/sessions` | List / search session history |
| `/compact` | Compact conversation context |
| `/context` | Show token usage + cost |
| `/model <name>` | Switch model at runtime |
| `/config` | Show current configuration |
| `/vision <img> [prompt]` | Analyze image |
| `/think` | Toggle thinking mode |
| `/permissions` | Show permission rules |
| `/git <sub>` | Git operations |
| `/plan <task>` | Task planning |
| `/remember` / `/recall` | Memory management |
| `/mcp search <q>` | Search MCP tools |

## Project Structure

```
ata_coder/
├── main.py                  # CLI entry point (click) + asyncio.run()
├── agent.py                 # Core agent: async run loop, event system, session mgmt
├── agent_tools.py           # ToolExecutionMixin — tool dispatch, streaming, self-correct
├── agent_compact.py         # CompactionMixin — LLM summarization + force truncate
├── agent_controller.py      # asyncio.Task-based orchestrator
├── agent_routing.py         # ModelRoutingMixin — keyword+length task classification
├── agent_extension.py       # ExtensionMixin — extension registration + lifecycle
├── agent_subsystems.py      # AgentSubsystems dataclass
├── context_manager.py       # O(1) token tracking, segment-split, compaction
├── core/                    # AgentEvent, AgentState, EventQueue
├── tools/                   # 14 tool handlers + executor
├── commands/                # Slash commands (/help /skills /model /context etc.)
├── llm_client.py            # OpenAI-compatible async client (httpx.AsyncClient)
├── anthropic_client.py      # Anthropic Messages API async client
├── skills.py                # Folder-based skill manager
├── extension.py             # Plugin/extension system
├── sub_agent.py             # asyncio.Task-based sub-agent
├── mcp_client.py            # Async MCP (stdio + HTTP/SSE)
├── memory.py                # Persistent file-based memory
├── session.py               # Session save/load/search
├── change_tracker.py        # File change undo/redo
├── safety_guard.py          # Pattern-based risk analysis
├── fool_proof.py            # Unified pre-execution safety check
├── permissions.py           # Interactive allow/deny rules
├── privilege.py             # OS-aware privilege elevation
├── config.py                # Runtime config (reads settings.json only)
├── settings.py              # ~/.ata_coder/settings.json persistence
├── project.py               # Auto-detect language/framework/style/git
├── system_prompt_builder.py # Dynamic prompt assembly
├── model_registry.py        # Model metadata + pricing
├── token_counter.py         # Unified token estimation (model-aware, cached)
├── repl_ui.py               # Rich/prompt-toolkit REPL + diff preview
├── server.py                # HTTP API server + SSE streaming
├── server_session.py        # SessionStore for multi-session management
├── server_shell.py          # Persistent PowerShell/bash sessions
├── server_routes.py         # REST endpoint handlers
├── server_sse.py            # SSE event serialization
├── server_rate_limit.py     # Token bucket rate limiter
├── prompt_template.py       # {% if %} templating engine
├── git_workflow.py          # Git integration
├── setup_wizard.py          # First-run setup wizard
├── skills/                  # Built-in skill folders
├── extensions/              # Plugin directory
├── ts-server/               # TypeScript companion (Node.js 24 native TS)
├── tests/                   # pytest suite (566 tests)
└── README.md
```

## Testing

```bash
pytest                                  # All tests
pytest tests/ --ignore=tests/test_server.py   # Windows-safe
pytest tests/test_tools.py -q                 # Single file
pytest -k "agent" -q                          # Filter by name
```

## Contributing

> 👋 **Want to dive in?** Whether it's your first PR or your hundredth, we've written a detailed participation guide just for you.

<p align="center">
  <strong>👉 <a href="./CONTRIBUTING.md">CONTRIBUTING.md</a> — The Official Participation Handbook</strong>
</p>

It covers everything: dev environment setup, architecture walkthrough, the iron rules for changes, commit format, testing strategy, code review checklist, and the release process. Read it before your first commit — it'll save you a round of review.

A quick taste of what you'll find there:

- **One problem per change.** No drive-by refactoring.
- **≤ 3 files, ≤ 200 lines per PR.** We enforce this mechanically.
- **Never delete comments.** Append corrections; don't overwrite history.
- **Commit messages follow a three-part format.** No "fix bug" one-liners.

## License

MIT © 2024–2026 ATA Coder Team. See [LICENSE](LICENSE) for full text.
