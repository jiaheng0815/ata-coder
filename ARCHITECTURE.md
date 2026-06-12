# ATA Coder — Architecture Overview

## Project Summary

ATA Coder is a multi-threaded, multi-skill, plugin-driven AI coding assistant CLI. It wraps any OpenAI/Anthropic-compatible LLM with a full safety pipeline, persistent memory, session history, and a sub-agent pool for parallel task execution.

---

## Directory Tree

```
ata_coder/
│
├── main.py                   # CLI entry (click): interactive / single / server
├── agent.py                  # Core agent loop: LLM chat -> tool calls -> results
├── agent_controller.py       # Background-thread orchestrator + EventQueue + heartbeat
├── agent_subsystems.py       # DI container for all subsystems
├── system_prompt_builder.py  # Compose system prompt from skills/memory/tools/env
│
├── llm_client.py             # OpenAI-compatible sync HTTP client
├── anthropic_client.py       # Anthropic Messages API sync HTTP client
├── model_registry.py         # Model metadata, pricing, URL building
├── model_router.py           # AI-driven complexity routing (simple/complex)
│
├── tools.py                  # 12 built-in tools (read/write/edit/shell/grep/glob/...)
├── mcp_client.py             # MCP protocol client for cross-tool interop
│
├── skills.py                 # Folder-based skill manager (SKILL.md, handlers)
├── skill_extension.py        # Skill -> Extension adapter
├── extension.py              # Plugin system: Extension ABC, ExtensionPoint, Manager
│
├── sub_agent.py              # Independent sub-agent with isolated context
├── sub_agent_manager.py      # Concurrent sub-agent pool (spawn/collect/cancel)
├── event_queue.py            # Thread-safe agent->UI event bus
├── thread_supervisor.py      # Health monitoring + timeout fencing
│
├── memory.py                 # Persistent file-based memory store
├── session.py                # Session save/load/search/export (JSONL + index)
├── change_tracker.py         # File change undo/redo with auto-backup
│
├── safety_guard.py           # Content safety filter (forbidden patterns)
├── fool_proof.py             # Risky operation detection (BLOCK/CONFIRM/PASS)
├── permissions.py            # Interactive allow/deny per category
├── privilege.py              # OS privilege detection + elevation
├── self_correct.py           # Tool call auto-retry with diagnosis
│
├── config.py                 # Runtime config from env vars
├── settings.py               # Persistent settings (~/.ata_coder/settings.json)
├── repl_ui.py                # Rich-based REPL + One Dark Pro dark theme
├── commands.py               # Slash command registry (/help, /history, ...)
├── server.py                 # HTTP API server (Flask-style)
├── project.py                # Language/framework/build-system auto-detection
├── prompt_template.py        # Jinja-style prompt template engine
├── task_planner.py           # LLM-based task decomposition
├── git_workflow.py           # Session-aware git commit/branch/diff
├── test_runner.py            # Auto-detect + run project tests
│
├── skills/                   # Skill folders (each = one SKILL.md)
│   ├── general-coder/        # General-purpose coding
│   ├── debugger/             # Bug diagnosis
│   ├── code-reviewer/        # Code review
│   ├── architect/            # Architecture design
│   ├── test-writer/          # Test generation
│   ├── doc-writer/           # Documentation
│   ├── security-auditor/     # Security audit
│   ├── codecraft/            # Coding standards reference
│   ├── math-calculator/      # Safe math eval (full example)
│   └── weather-skill/        # Weather API (full example)
│       ├── SKILL.md          #   Manifest: name, version, I/O, permissions
│       ├── handler.py        #   run(input_data) entry
│       ├── utils.py          #   Helpers
│       ├── prompts/          #   LLM prompt templates
│       ├── resources/        #   Static data (JSON)
│       ├── tests/            #   pytest
│       ├── requirements.txt  #   Dependencies
│       └── README.md         #   Usage docs
│
├── extensions/               # Plugin directory (auto-discovered)
│   ├── hello_skill.py        #   Example extension
│   └── README.md             #   Plugin API docs
│
├── prompts/                  # System prompt templates
├── examples/                 # Usage examples
├── memory/                   # Persistent memory files
├── tests/                    # pytest suite (405 tests)
├── LICENSE                   # MIT
├── README.md                 # Project docs (CN + EN)
├── ARCHITECTURE.md           # This file
├── API.md                    # API docs
└── pyproject.toml            # Build config
```

---

## Thread Architecture

```
Main/UI Thread (main.py)
  |
  |  prompt_toolkit REPL
  |  AgentController.submit(task)
  |  while busy:
  |    drain EventQueue -> ui.on_event()
  |    check cancel
  |
  +-- Agent Thread (agent_controller.py, daemon)
  |     |
  |     |  wait on input_queue
  |     |  CoderAgent.run(task)
  |     |    -> LLM chat loop (blocking httpx)
  |     |    -> tool calls (sync subprocess / file I/O)
  |     |    -> events -> EventQueue
  |     |  heartbeat every start/end of task
  |     |
  |     +-- Heartbeat Pumper (daemon)
  |           pulse heartbeat every 30s while busy
  |           idle: block on _cancel.wait(5s)
  |
  +-- Sub-Agent Threads (daemon, up to 5 concurrent)
  |     |
  |     |  SubAgent each:
  |     |    own LLM client (separate httpx session)
  |     |    own ToolExecutor
  |     |    isolated message history
  |     |    cancel via threading.Event
  |     |
  |     +-- SubAgentManager
  |           spawn / collect / cancel_all / list
  |           max_concurrent enforcement
  |
  +-- Watchdog Thread (thread_supervisor.py, daemon)
        every 1s:
          check all registered heartbeats
          timeout (1800s) -> fence via cancel event
          rate-limited logging (30s between alerts)
```

---

## Agent Run Pipeline

```
User Input
  |
  v
Skill Detection (skills.py)
  trigger keyword matching -> top-3 skills
  auto-activate with merge=True
  |
  v
Model Routing (agent.py)
  AI classify -> simple/complex/normal
  route to haiku/opus/default model
  |
  v
System Prompt Build (system_prompt_builder.py)
  base:  active skills aggregated by priority (ExtensionManager)
  + environment (OS, Python, model, date)
  + project (languages, git, framework)
  + tools list (builtin + MCP + extension)
  + memory (targeted recall from user input)
  + ops notes + formatting guide
  |
  v
Main Loop (agent.py)
  while tool_calls and count < SAFETY_LIMIT:
    |
    +-> Token Check
    |     if > effective_context (200k): compact()
    |
    +-> Tool Filter
    |     _compute_allowed_tools() = intersection of all active skill restrictions
    |
    +-> LLM Call (streaming or batch)
    |     llm_client.chat_stream() -> yields TextDelta / ToolCall / Reasoning
    |     events -> EventQueue -> UI thread renders
    |
    +-> Tool Execution
    |     _can_parallelize()?
    |       YES -> ThreadPoolExecutor(max=4)
    |       NO  -> serial for loop
    |     Each tool:
    |       fool_proof.evaluate()   (BLOCK/CONFIRM/PASS)
    |       permissions.check()     (user prompt if needed)
    |       privilege.check()       (elevation wrapping)
    |       execute()               (builtin or MCP)
    |       self_correct.retry()    (diagnosis + fix on failure)
    |       change_tracker.capture()(record for undo)
    |       extension point trigger (on_tool_execute / on_tool_result)
    |
    +-> Store in messages
    |
  v
Post-Loop
  emit CompleteEvent
  memory auto-suggestions
  extension point: on_run_complete
  save session
```

---

## Data Flow

```
Settings (~/.ata_coder/)
  settings.json -> model defaults, paths, skill seeds
  skills/       -> skill folders loaded by SkillManager
  sessions/     -> conversation JSONL files
  sessions.json -> session index for search
  changes/      -> file change backups per session
  memory/       -> persistent memory .md files with MEMORY.md index

Runtime Config (config.py)
  env vars + .env files -> AppConfig{LLMConfig, AgentConfig}

Extension Pipeline
  Skill (.md) -> Skill.from_frontmatter() -> SkillExtension -> ExtensionManager.register()
  ExtensionManager.activate() -> on_activate() -> get_prompt() added to system prompt
  agent._compute_allowed_tools() -> intersection of all active skill.tools lists
```

---

## Safety Layers (from outermost to innermost)

```
1. safety_guard.py      Content filter: forbidden patterns, suspicious file extensions
2. fool_proof.py        Risky operation detection: BLOCK / CONFIRM / PASS
3. permissions.py       Interactive allow/deny per category (shell, write, network)
4. privilege.py         OS privilege detection + elevation wrapping
5. change_tracker.py    Undo/redo + file backup before every write
6. self_correct.py      Tool call auto-retry on failure (max 3, with diagnosis)
7. SAFETY_LIMIT = 999   Circuit breaker on tool call count
```

---

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **sync, not async** | httpx sync + daemon threads = simpler than full async rewrite; LLM I/O is the bottleneck, GIL doesn't matter |
| **EventQueue, not callback** | Thread-safe agent->UI decoupling; Rich/prompt_toolkit stay on main thread |
| **Skills as Extensions** | SkillExtension adapter wraps Skill -> unified plugin lifecycle |
| **Tool intersection for multi-skill** | Most restrictive wins; empty=all (no restriction) |
| **Sub-Agent own LLM client** | Isolated httpx session + message list = no context leakage |
| **Heartbeat Pumper** | Separate daemon thread pulses heartbeat while agent is busy; avoids false timeout during long LLM calls |
| **Rate-limited Watchdog logging** | First timeout: WARNING. Subsequent: DEBUG every 30s. No spam. |
| **One Dark Pro theme** | Dark background (#282C34), syntax colors (keywords=#E06C75, functions=#98C379, strings=#E5C07B, numbers=#56B6C2) |
