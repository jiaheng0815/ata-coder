# Changelog

## v1.0.0 (2026-06-21)

First official release.

**Core:**
- Async AI agent with OpenAI and Anthropic API support
- Interactive REPL (prompt_toolkit + Rich), single-task mode, HTTP API server mode
- Streaming LLM responses with real-time tool output
- O(1) token tracking with auto-compaction and LLM summarization
- Enhanced system prompt (180-line engineering agent persona)

**Tools (14):**
- File operations: read_file, write_file, edit_file (with colorized diff preview)
- Shell: run_shell (timeout-protected, output-capped)
- Search: grep, glob, web_search (DuckDuckGo), web_fetch
- Sub-agents: spawn_subagent, collect_subagent (parallel isolated execution)
- MCP: mcp_search
- Vision: analyze_image
- AST editing: rename_symbol (libcst)

**Safety:**
- Multi-layer pipeline: pattern guard → fool-proof check → permissions → privilege
- Interactive allow/deny/ask per tool category
- Change tracking with undo/redo

**Skills & Extensions:**
- Folder-based skill system with keyword auto-detection
- Plugin/extension system with pub/sub hooks

**Server:**
- HTTP API with SSE streaming, multi-session management
- Persistent shell sessions, rate limiting

**TypeScript Companion (optional, Node.js ≥ 24):**
- CLI, HTTP/SSE, MCP bridge, shell manager, safety guard, sessions, git, project detection
- JSON-RPC subprocess IPC with Python core

**Project Awareness:**
- Auto-detects language, framework, build system, test framework
- Auto-reads CLAUDE.md, samples code style, shows recent git activity

**Tests:** 566
