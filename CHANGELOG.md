# Changelog

## v1.0.1 (2026-06-22)

Bug fixes, test coverage, and quality improvements.

**Bug fixes (6):**
- Session resume broken: `reset_context=True` wiped restored messages before LLM saw them
- Session resume broken: `_context_manager` token tracking not synced after resume
- Session ID displayed as task-hash-only (8 hex), causing false duplicates for same task
- CLAUDE.md persona ("sweetheart"/"code witch") injected verbatim into LLM system prompt
- `_check_secrets` pattern order: generic API key pattern captured GitHub/OpenAI/AWS keys before their specific patterns
- OpenAI key regex `sk-[A-Za-z0-9]{32,}` didn't match real keys with dashes (`sk-proj-...`)

**Test coverage (+242 tests, 5→27 LLM client, 0→33 anthropic client, 0→31 context_manager, 0→29 self_correct, 0→29 git_workflow, 0→21 utils, 0→15 session):**
- `test_llm_client.py`: retry delay, surrogate sanitization, tool-call assembly, state management
- `test_anthropic_client.py`: JSON balancing, message/tool format conversion, streaming events
- `test_context_manager.py`: O(1) token tracking, segment splitting, compaction decisions
- `test_self_correct.py`: error diagnosis, fix suggestion, retry tracking, session learning
- `test_git_workflow.py`: secret detection, commit message generation, branch sanitization
- `test_utils.py`: deep_merge_dict, brief_args, enhance_api_error, sanitize_surrogates edges
- `test_session.py`: SessionMeta serialization, generate_session_id, CRUD + export

**Other:**
- Fixed flaky `test_count_tokens_cached_speed` (was testing wrong cache layer)
- Fixed `test_fibonacci.py` import error (missing `examples/__init__.py` + `tests/conftest.py`)
- GitHub repo metadata: description + 12 topics
- Tests: 774 passed (was 532)

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
