# ATA Coder

**AI-powered coding assistant with multi-skill, multi-agent, and plugin architecture.**

[English](#english) | [中文](#中文)

---

## English

### Overview

ATA Coder is a full-featured CLI coding assistant that integrates with any OpenAI-compatible or Anthropic API. It supports interactive REPL mode, single-task mode, and HTTP API server mode.

### Key Features

| Feature | Description |
|---------|-------------|
| **Multi-Skill** | Multiple skills active simultaneously with priority-based prompt aggregation |
| **Extension System** | Plugin architecture with lifecycle hooks, ExtensionPoints, and auto-discovery |
| **Sub-Agent Pool** | Spawn independent sub-agents with isolated context windows for parallel work |
| **Multi-Threaded** | Agent runs on background thread; ThreadSupervisor prevents single-thread crashes |
| **Folder-Based Skills** | Skills organized as folders with `SKILL.md`, `handler.py`, `prompts/`, `resources/` |
| **Memory System** | Persistent file-based memory with targeted recall across sessions |
| **Session History** | Search, resume, and export conversation history by workspace |
| **Safety Pipeline** | Fool-proof validation, permission prompts, privilege management, change tracking |
| **MCP Support** | Model Context Protocol for cross-tool interoperability |
| **Git Integration** | Auto-detect git repos, session-aware commits, undo/redo changes |
| **One Dark Pro Theme** | Custom dark theme with syntax highlighting for code blocks |

### Quick Start

```bash
pip install -e .
ata                          # Interactive mode
ata run "Add type hints"     # Single task
ata server --port 8080       # API server
ata --skill debugger         # With specific skill
```

### Architecture

```
Main/UI Thread
  prompt_toolkit REPL -> AgentController.submit()
  EventQueue drain -> ui.on_event()
Agent Thread (agent_controller.py)
  CoderAgent.run() -> LLM chat loop -> tool calls
  ExtensionManager -> multi-skill prompt aggregation
Sub-Agent Threads
  SubAgent 1..N: independent LLM, tools, context
Watchdog (thread_supervisor.py)
  heartbeat -> timeout detection -> fence
```

### Project Structure

```
ata_coder/
├── main.py              # CLI entry point (click)
├── agent.py             # Core agent loop
├── agent_controller.py  # Background-thread orchestrator
├── agent_subsystems.py  # Subsystem container
├── system_prompt_builder.py
├── skills.py            # Folder-based skill manager
├── skill_extension.py   # Skill -> Extension adapter
├── extension.py         # Plugin/extension system
├── sub_agent.py         # Independent sub-agent
├── sub_agent_manager.py # Concurrent sub-agent pool
├── event_queue.py       # Thread-safe event queue
├── thread_supervisor.py # Health monitoring + fencing
├── tools.py             # 12 built-in tools
├── llm_client.py        # OpenAI-compatible client
├── anthropic_client.py  # Anthropic Messages API client
├── memory.py            # Persistent memory store
├── session.py           # Session save/load/search
├── change_tracker.py    # File change undo/redo
├── safety_guard.py      # Content safety filter
├── fool_proof.py        # Risky operation detection
├── permissions.py       # Interactive allow/deny
├── privilege.py         # OS privilege detection
├── config.py            # Runtime configuration
├── settings.py          # Persistent settings (~/.ata_coder/)
├── model_registry.py    # Model metadata + pricing
├── model_router.py      # AI-driven model selection
├── commands.py          # Slash command registry
├── repl_ui.py           # Rich-based REPL UI + dark theme
├── server.py            # HTTP API server
├── project.py           # Project auto-detection
├── skills/              # Skill folders
├── extensions/          # Plugin directory
├── prompts/             # Prompt templates
├── examples/            # Usage examples
├── tests/               # pytest test suite
├── LICENSE              # MIT License
└── README.md            # This file
```

### Skill System

Skills are folders under `skills/`:

```
skills/weather-skill/
├── SKILL.md           # Manifest: name, version, I/O, permissions
├── handler.py         # run(input_data) entry point
├── utils.py           # Helper functions
├── prompts/           # LLM prompt templates
├── resources/         # Static data (JSON, YAML)
├── tests/             # pytest tests
├── requirements.txt   # Dependencies
└── README.md          # Usage docs
```

Manifest priority: `SKILL.md` > `manifest.json` > `skill.yaml`

### Commands

| Command | Description |
|---------|-------------|
| `/help` | Show help |
| `/history [n\|keyword]` | Browse/search history |
| `/skills` | List available skills |
| `/skill <name>` | Switch skill |
| `/review` | AI code review of changes |
| `/undo [n]` | Undo file changes |
| `/changes` | List tracked changes |
| `/save` | Save current session |
| `/resume <id>` | Resume saved session |
| `/extensions` | List loaded extensions |
| `/sub-agents` | List sub-agents |
| `/model <name>` | Change model |
| `/workspace <path>` | Change workspace |
| `/context` | Show token usage |

### Configuration

```bash
# Environment variables
ATA_CODER_API_KEY=sk-...
ATA_CODER_BASE_URL=https://api.deepseek.com
ATA_CODER_DEFAULT_MODEL=deepseek-v4-pro
WORKSPACE_DIR=/path/to/project
MAX_SUB_AGENTS=5
```

---

## 中文

### 概述

ATA Coder 是一个功能完备的命令行 AI 编码助手，兼容任何 OpenAI 格式或 Anthropic API。支持交互式 REPL、单任务模式和 HTTP API 服务模式。

### 核心特性

| 特性 | 说明 |
|------|------|
| **多技能并存** | 多个 Skill 同时激活，按优先级聚合提示词 |
| **扩展系统** | 插件架构：生命周期钩子、ExtensionPoint 事件系统、自动发现 |
| **子 Agent 池** | 独立上下文的子 Agent，并行执行，主对话隔离 |
| **多线程架构** | Agent 后台线程运行，ThreadSupervisor 防止单线程崩溃 |
| **文件夹 Skill** | Skill 以文件夹组织，含 SKILL.md、handler.py、prompts/、resources/ |
| **记忆系统** | 持久化文件记忆，跨会话定向召回 |
| **会话历史** | 按工作区搜索、恢复、导出历史对话 |
| **安全管线** | 防呆验证、权限提示、特权管理、变更追踪 |
| **MCP 支持** | Model Context Protocol 跨工具互操作 |
| **Git 集成** | 自动检测 git 仓库，撤销/重做变更 |
| **暗色主题** | One Dark Pro 自定义暗色主题 + 语法高亮 |

### 快速开始

```bash
pip install -e .
ata                          # 交互模式
ata run "添加类型注解"        # 单任务
ata server --port 8080       # API 服务
ata --skill debugger         # 指定技能
```

### 架构

```
主线程 / UI
  prompt_toolkit REPL -> AgentController.submit()
  EventQueue 消费 -> ui.on_event()
Agent 线程 (agent_controller.py)
  CoderAgent.run() -> LLM 对话循环 -> 工具调用
  ExtensionManager -> 多 Skill 提示聚合
子 Agent 线程池
  SubAgent 1..N: 独立 LLM、工具、上下文
看门狗 (thread_supervisor.py)
  心跳监控 -> 超时检测 -> 熔断
```

### 常用命令

| 命令 | 说明 |
|------|------|
| `/help` | 显示帮助 |
| `/history [n\|关键词]` | 浏览/搜索对话历史 |
| `/skills` | 列出可用 Skill |
| `/skill <名称>` | 切换 Skill |
| `/review` | AI 审查代码变更 |
| `/undo [n]` | 撤销文件变更 |
| `/save` | 保存当前会话 |
| `/resume <id>` | 恢复已保存会话 |
| `/extensions` | 列出已加载扩展 |
| `/sub-agents` | 列出子 Agent |

### 配置

```bash
ATA_CODER_API_KEY=sk-...
ATA_CODER_BASE_URL=https://api.deepseek.com
ATA_CODER_DEFAULT_MODEL=deepseek-v4-pro
WORKSPACE_DIR=/项目/路径
MAX_SUB_AGENTS=5
```

---

## License

MIT — see [LICENSE](LICENSE)
