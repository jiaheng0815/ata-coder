# ATA Coder

**CLI AI coding assistant — OpenAI & Anthropic APIs, async, streaming, built-in HTTP server.**

---

## Overview

ATA Coder is a terminal-based AI coding assistant. Describe what you want in plain language — the AI reads your code, suggests edits, runs shell commands, and searches the web. Every destructive action asks for confirmation first.

```bash
pip install ata-coder
```

Requires Python 3.10+. That's the only runtime dependency.

## Quick Start

```bash
ata                              # Interactive REPL
ata run "Fix the timeout bug"    # Single-shot task
ata server --port 8080           # HTTP API + SSE streaming
ata --skill debugger             # With a specific skill
ata --resume <session-id>        # Resume a saved session
```

First run walks you through API key and model setup. Config saved to `~/.ata_coder/settings.json`.

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

[AI reads the file, identifies that httpx.Timeout has separate connect/read/write
 phases, suggests a fix →]

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

## Key Features

| Feature | Description |
|---------|-------------|
| **Async agent** | Single-threaded asyncio event loop. Streaming LLM responses. |
| **File tools** | `read_file`, `write_file`, `edit_file` with colorized diff preview. |
| **Shell execution** | `run_shell` — output capped at 500KB, timeout-protected. |
| **Code search** | `grep` (regex) and `glob` (file patterns) — dedicated tools, not shell hacks. |
| **Web search** | `web_search` (DuckDuckGo) and `web_fetch` (URL text extraction). No API key needed. |
| **Sub-agents** | Parallel isolated agents for complex multi-step tasks. Semaphore-bounded pool. |
| **Skill system** | Folder-based skills with auto-detection. Single-skill activation for clean prompts. |
| **MCP support** | Model Context Protocol — stdio and HTTP/SSE transport. |
| **Vision** | `analyze_image` — auto-falls back to main API config. |
| **Session persistence** | Save / resume / export conversation history. |
| **Safety pipeline** | Pattern-based guard → fool-proof check → interactive permissions → OS privilege elevation. |
| **Change tracking** | Undo/redo file changes with session-level backups. |
| **Configuration** | Single `settings.json` file. No env var reading in code — all config flows through settings. |
| **HTTP API server** | SSE streaming, persistent shell sessions, multi-session management. |
| **TypeScript companion** | Node.js 24 native TS — CLI, HTTP, MCP bridge, shell manager, safety guard, session/memory store, git, project detection. (Optional — Python core works standalone.) |
| **Web GUI** | SPA with SSE streaming, markdown rendering, sidebar, command popup. |
| **Diff preview** | Colorized unified diff in terminal before every file edit. |
| **Token management** | O(1) token tracking, auto-compaction with LLM summarization, force-truncate fallback. |
| **Project auto-detection** | Reads CLAUDE.md, detects language/framework/build-system/test-framework, samples code style, shows recent git activity. |

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

The Python core (`agent.py`, LLM clients, tool executor, skills) handles AI/LLM logic. The TypeScript companion (`ts-server/`, Node.js ≥ 24) handles HTTP/SSE/shell/MCP/safety/sessions — communicates via JSON-RPC subprocess IPC.

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

## API Server

```bash
ata server --port 8080
```

| Endpoint | Description |
|----------|-------------|
| `POST /chat` | Non-streaming chat |
| `POST /chat/stream` | SSE streaming chat |
| `GET /health` | Health check |
| `GET /tools` | List tools |
| `GET /skills` | List skills |
| `GET /models` | List available models |
| `GET /sessions` | List sessions |
| `DELETE /sessions/<id>` | Delete session |
| `POST /api/shell` | Interactive shell |

```bash
# Streaming
curl -N -X POST http://localhost:8080/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"message": "Explain async/await"}'

# Non-streaming
curl -X POST http://localhost:8080/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Write hello world", "skill": "general-coder"}'
```

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

Works with any OpenAI-compatible API: OpenAI, DeepSeek, Anthropic (via compatible gateway), OpenRouter, Ollama, and more.

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
├── llm_client.py            # OpenAI-compatible async client
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
├── clawd_integration.py     # Clawd desktop pet HTTP integration
├── gui.py                   # Tkinter GUI
├── setup_wizard.py          # First-run setup wizard
├── skills/                  # Built-in skill folders
├── extensions/              # Plugin directory
├── ts-server/               # TypeScript companion (Node.js 24 native TS)
├── tests/                   # pytest suite (566 tests)
└── README.md
```

## Skills

Skills live in `skills/<name>/` folders with `SKILL.md` manifest:

```
skills/weather-skill/
├── SKILL.md           # name, version, triggers, tools, I/O spec
├── handler.py         # Python entry point
├── utils.py           # Helpers
├── prompts/           # LLM prompt templates
├── resources/         # Static data
└── tests/             # pytest tests
```

## Contributing

### Quick Start

```bash
git clone https://github.com/jiaheng0815/ata-coder.git
cd ata-coder
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"
pytest tests/ --ignore=tests/test_server.py -q
```

### Development Rules

These rules keep the codebase manageable for both human and AI contributors:

1. **One problem per change** — no refactoring alongside bugfixes.
2. **No defensive coding** — don't add null-checks for unreproduced failures.
3. **Never delete comments** — append corrections below, don't overwrite.

| Limit | Value |
|-------|-------|
| Files per change | ≤ 3 |
| Lines added+deleted | ≤ 200 |
| McCabe per new function | ≤ 10 |
| New dependencies | Zero without approval |

### Commit Format

```
fix: [problem] -> [expected behavior]

回滚方案：若合并后出现异常，请执行 git revert HEAD 无损回退。

变更列表：
- file.py: function_name — brief description
```

## Testing

```bash
pytest tests/ --ignore=tests/test_server.py     # Windows-safe
pytest tests/test_tools.py -q                    # Single file
pytest -k "agent" -q                             # Filter by name
```

## License

MIT
