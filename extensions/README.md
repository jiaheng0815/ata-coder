# Extensions

Put your custom extensions here. Each `.py` file is auto-discovered
by `ExtensionManager.discover("./extensions/")`.

## Creating an Extension

```python
from ata_coder.extension import Extension, ExtensionMeta, extension

@extension(
    name="my-skill",
    version="1.0.0",
    description="What this extension does",
    tags=["skill"],
    priority=80,
)
class MySkill(Extension):
    def get_prompt(self) -> str:
        return "You are an expert in..."

    def get_tools(self) -> list:
        return []  # tool names to restrict; empty = all allowed

    def on_activate(self) -> None:
        pass

    def on_deactivate(self) -> None:
        pass
```

## Extension API

| Method | Returns | Purpose |
|--------|---------|---------|
| `get_prompt()` | `str` | System prompt fragment |
| `get_tools()` | `list[str]` | Tool restriction list (empty=all) |
| `get_middleware()` | `list[Callable]` | Middleware hooks |
| `validate()` | `(bool, str)` | Pre-flight validation |
| `on_activate()` | `None` | Called on activation |
| `on_deactivate()` | `None` | Called on deactivation |

## Extension Types

| Tag | Purpose |
|-----|---------|
| `skill` | Persona/prompt modifier |
| `tool` | Adds new tool definitions |
| `middleware` | Intercepts tool calls |
| `mcp` | MCP server integration |
