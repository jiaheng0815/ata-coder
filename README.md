# 🤖 ATA Coder

An AI-powered coding assistant that uses OpenAI-compatible APIs. It can read, write, edit, search, and execute commands in your project — all driven by an LLM with function calling.

## Features

- **OpenAI-compatible** — works with OpenAI, Azure, local LLMs (Ollama, vLLM, LM Studio), and any provider with the chat completions API format
- **Tool use** — read files, write files, edit files, grep search, glob patterns, directory listing, shell commands
- **Streaming** — real-time text output as the agent thinks
- **Interactive & single-task modes** — use as a CLI tool or in interactive REPL
- **Configurable** — model, API key, base URL, workspace, all via env vars or CLI flags
- **Safe** — command allowlisting, blocked patterns, workspace scoping

## Installation

```bash
pip install -r requirements.txt
```

## Quick Start

1. Copy the example environment file:
```bash
cp .env.example .env
```

2. Edit `.env` and add your API key:
```
OPENAI_API_KEY=sk-your-key-here
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o
```

3. Run the agent:
```bash
# Interactive mode
python main.py

# Single task mode
python main.py "Add type hints to all Python files in this project"

# With CLI options
python main.py --model gpt-4o --workspace /path/to/project "Explain this codebase"
```

## Usage

### CLI Arguments

| Argument | Description |
|----------|-------------|
| `task` | Task to execute (optional, defaults to interactive mode) |
| `--model`, `-m` | Model name |
| `--api-key`, `-k` | API key |
| `--base-url`, `-b` | API base URL |
| `--workspace`, `-w` | Workspace directory |
| `--no-stream`, `-n` | Disable streaming |
| `--verbose`, `-v` | Enable debug logging |
| `--max-tool-calls` | Max tool calls per task (default: 30) |

### Interactive Commands

| Command | Description |
|---------|-------------|
| `/help` | Show help |
| `/quit`, `/exit` | Exit the agent |
| `/clear` | Clear conversation history |
| `/summary` | Show conversation summary |
| `/model <name>` | Change model |
| `/workspace <path>` | Change workspace directory |

### Using with Ollama

```bash
export OPENAI_API_KEY=ollama
export OPENAI_BASE_URL=http://localhost:11434/v1
export OPENAI_MODEL=qwen2.5-coder:14b
python main.py
```

### Using with DeepSeek

```bash
export OPENAI_API_KEY=sk-your-deepseek-key
export OPENAI_BASE_URL=https://api.deepseek.com/v1
export OPENAI_MODEL=deepseek-chat
python main.py
```

## Architecture

```
ata_coder/
├── main.py          # CLI entry point, interactive loop, argument parsing
├── agent.py         # Core agent loop, conversation management, tool dispatch
├── llm_client.py    # OpenAI-compatible API client, streaming, function calling
├── tools.py         # Tool definitions and implementations (file, shell, search)
├── config.py        # Configuration from env vars, CLI, .env file
├── requirements.txt # Python dependencies
└── .env.example     # Environment variable template
```

### How It Works

1. **User input** → The user provides a coding task
2. **LLM call** → The agent sends the task + conversation history to the LLM
3. **Tool calls** → If the LLM decides to use a tool, the agent executes it
4. **Feedback loop** → Tool results go back into the conversation
5. **Completion** → The LLM produces a final text response

### Available Tools

| Tool | Description |
|------|-------------|
| `read_file` | Read file contents with line numbers |
| `write_file` | Create or overwrite a file |
| `edit_file` | Exact string replacement in a file |
| `run_shell` | Execute shell commands (with safety checks) |
| `grep` | Search file contents with regex |
| `glob` | Find files by glob pattern |
| `list_dir` | List directory contents |

## Configuration Reference

| Env Variable | Default | Description |
|-------------|---------|-------------|
| `OPENAI_API_KEY` | — | API key (required) |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | API endpoint |
| `OPENAI_MODEL` | `gpt-4o` | Model name |
| `TEMPERATURE` | `0.1` | Response creativity |
| `MAX_OUTPUT_TOKENS` | `16384` | Max response tokens |
| `MAX_TOOL_CALLS` | `30` | Max tool calls per task |
| `MAX_CONTEXT_TOKENS` | `1000000` | Theoretical context window (1M) |
| `EFFECTIVE_CONTEXT_TOKENS` | `200000` | Effective attention range (auto-compact trigger) |
| `WORKSPACE_DIR` | current directory | Working directory |

## License

MIT
