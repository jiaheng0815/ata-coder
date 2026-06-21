"""Tool definitions (OpenAI function format)."""

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file. Returns the file content with line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "The absolute or relative path to the file to read.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Line number to start reading from (1-based).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of lines to read.",
                    },
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file. Creates the file if it doesn't exist, overwrites if it does.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "The absolute or relative path to the file to write.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The content to write to the file.",
                    },
                },
                "required": ["file_path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Perform exact string replacement in a file. The old_string must match exactly (including whitespace/indentation) and be unique in the file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "The absolute or relative path to the file to edit.",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "The exact text to find and replace.",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "The text to replace it with.",
                    },
                },
                "required": ["file_path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rename_symbol",
            "description": "Safely rename a Python symbol (function, method, class, variable) — updates definition and all references, skipping comments and strings. Uses AST-aware matching.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "The absolute or relative path to the Python file.",
                    },
                    "old_name": {
                        "type": "string",
                        "description": "Current symbol name to rename.",
                    },
                    "new_name": {
                        "type": "string",
                        "description": "New symbol name.",
                    },
                    "symbol_type": {
                        "type": "string",
                        "enum": ["function", "class", "variable", "method"],
                        "description": "Type of symbol being renamed.",
                    },
                },
                "required": ["file_path", "old_name", "new_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Execute a shell command and return stdout/stderr. Use for build, test, lint, git, and other development commands.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default: 120).",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents using regular expressions. Returns matching files and lines.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "The regex pattern to search for.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory or file to search in. Defaults to current directory.",
                    },
                    "glob": {
                        "type": "string",
                        "description": "Filter files by glob pattern (e.g. '*.py', 'src/**/*.ts').",
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "description": "Whether search is case-sensitive (default: false).",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files matching a glob pattern. Returns sorted list of matching file paths.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern to match (e.g. '**/*.py', 'src/**/*.ts').",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory to search in. Defaults to current directory.",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List contents of a directory with file types and sizes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path to list. Defaults to current directory.",
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "Whether to list recursively (default: false).",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web with tiered fallback: Bing → Baidu → Google (no API key required). Returns titles, URLs, and snippets. Use when you need up-to-date information beyond your knowledge cutoff.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results (default: 10, max: 20).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch a web page and extract its text content. Strips HTML/scripts/CSS, returns plain text. Use after web_search to read the full content of a result. Caps at 15,000 characters.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to fetch (must be a full http/https URL).",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_subagent",
            "description": "Spawn a sub-agent to work on a task in parallel. The sub-agent runs in a background thread with its own isolated context window (no access to the main conversation history). Use for parallel searches, independent analysis, or delegating self-contained work. Returns immediately with the agent ID — use collect_subagent to get results. Max 5 concurrent sub-agents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "The task to delegate. Must be self-contained — the sub-agent has NO context from the main conversation. Be specific about what to do and what format to return results in.",
                    },
                    "skill": {
                        "type": "string",
                        "description": "Optional skill name for the sub-agent (e.g., 'code-reviewer', 'debugger', 'test-writer').",
                    },
                    "model": {
                        "type": "string",
                        "description": "Optional model override for the sub-agent. Use a cheaper/faster model for simple tasks.",
                    },
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "collect_subagent",
            "description": "Collect results from a previously spawned sub-agent. Blocks until the sub-agent completes or times out. The sub-agent's full message history is available in the result for context injection.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "The agent ID returned by spawn_subagent.",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Max wait time in seconds (default: 300).",
                    },
                },
                "required": ["agent_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_subagents",
            "description": "List all sub-agents and their statuses (running, done, failed, cancelled). Use to check on spawned sub-agents.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mcp_search",
            "description": "Search MCP (Model Context Protocol) tools and resources across all connected servers. Use this to find available MCP tools by keyword, or discover MCP resources (files, data, APIs) exposed by connected servers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search keyword. Matches against MCP tool names, descriptions, and resource URIs.",
                    },
                    "type": {
                        "type": "string",
                        "enum": ["tools", "resources", "all"],
                        "description": "What to search: 'tools' (default), 'resources', or 'all'.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_image",
            "description": "Analyze an image using a multimodal vision model. The vision provider is configured via VISION_MODEL / VISION_API_BASE / VISION_API_KEY environment variables (falls back to the main API config). Pass the path to an image file (PNG, JPG, GIF, WEBP) and a prompt describing what to look for. Returns a text description of the image content. Use this to read screenshots, photos, diagrams, charts, or any visual content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "image_path": {
                        "type": "string",
                        "description": "Absolute path to the image file to analyze.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "What to look for in the image. Be specific: 'Describe all UI elements in this screenshot', 'What text is visible?', 'Describe this diagram'.",
                    },
                },
                "required": ["image_path"],
            },
        },
    },
]

