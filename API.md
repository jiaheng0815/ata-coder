# ATA Coder API Reference

## 1. 扩展 API（`extension.py`）

正式插件系统，提供扩展的发现、注册、激活和生命周期管理。

### 1.1 Extension 基类

所有扩展需继承 `Extension` 并设置 `meta` 属性（可通过装饰器自动设置）。

```python
from ata_coder.extension import Extension, ExtensionMeta

class MyExtension(Extension):
    meta = ExtensionMeta(name="my-ext", version="1.0.0",
                         description="My custom extension")

    def on_load(self, manager: ExtensionManager) -> None:
        """扩展被加载到管理器时调用。"""

    def on_unload(self) -> None:
        """扩展被卸载时调用。"""

    def on_activate(self) -> None:
        """扩展被激活时调用。"""

    def on_deactivate(self) -> None:
        """扩展被停用时调用。"""

    def get_tools(self) -> list[dict]:
        """返回此扩展提供的工具定义（OpenAI 格式）。"""
        return []

    def get_prompt(self) -> str:
        """返回此扩展提供的系统提示片段（markdown）。"""
        return ""

    def validate(self) -> tuple[bool, str]:
        """验证扩展是否可用。返回 (ok, reason)。"""
        return True, "OK"
```

**生命周期:** `__init__` → `on_load(manager)` → `on_activate()` → `on_deactivate()` → `on_unload()`

### 1.2 @extension 装饰器

声明式注册扩展开关：

```python
from ata_coder.extension import extension, Extension

@extension(
    name="my-skill",
    version="1.0.0",
    description="A custom skill",
    tags=["skill"],
    priority=10,          # 越小越优先
    dependencies=[],      # 依赖的扩展名列表
)
class MySkill(Extension):
    def get_prompt(self) -> str:
        return "You are an expert in..."
```

### 1.3 ExtensionManager

管理所有扩展的加载、激活和卸载。

| 方法 | 说明 |
|---|---|
| `register(ext: Extension) -> bool` | 注册一个扩展 |
| `unregister(name: str) -> bool` | 注销一个扩展 |
| `activate(name: str) -> bool` | 激活一个扩展（自动激活依赖） |
| `deactivate(name: str) -> bool` | 停用一个扩展 |
| `discover(dir: Path) -> list[str]` | 从目录发现并加载扩展 |
| `get_extension(name: str) -> Extension?` | 按名称获取扩展 |
| `list_extensions() -> list[Extension]` | 列出所有已注册扩展 |
| `list_active() -> list[Extension]` | 列出所有已激活扩展 |
| `get_by_tag(tag: str) -> list[Extension]` | 按标签查询扩展 |
| `aggregate_prompts(base: str) -> str` | 聚合所有激活扩展的提示 |
| `aggregate_tools() -> list[dict]` | 聚合所有激活扩展的工具定义 |
| `stats() -> dict` | 返回扩展系统统计信息 |

### 1.4 ExtensionPoint

命名的可扩展点，模块定义、多个扩展注册处理器。

```python
from ata_coder.extension import ExtensionPoint

# 定义
ON_TOOL_CALL = ExtensionPoint("on_tool_call", "Fires before tool execution")

# 扩展注册
ON_TOOL_CALL.register(lambda tool_name, args: print(f"Tool: {tool_name}"))

# 触发
results = ON_TOOL_CALL.trigger(tool_name="read_file", args={})

# 拦截模式（第一个非 None 结果胜出）
result = ON_TOOL_CALL.trigger_first(tool_name="read_file", args={})
```

| 方法 | 说明 |
|---|---|
| `register(handler)` | 注册处理器到扩展点 |
| `unregister(handler)` | 移除处理器 |
| `trigger(*args, **kwargs) -> list` | 触发所有处理器，返回各返回值 |
| `trigger_first(*args, **kwargs) -> Any` | 返回第一个非 None 的结果 |
| `clear()` | 清空所有处理器 |

### 1.5 全局管理器

```python
from ata_coder.extension import get_extension_manager, reset_extension_manager

# 获取单例
mgr = get_extension_manager()

# 注册扩展点
mgr.extension_point("on_system_prompt", "Modify system prompt")
```

---

## 2. Agent API（`agent.py`）

核心 Agent 循环，整合 Skills、Memory、MCP、Templates、Permissions 等子系统。

### 2.1 CoderAgent

```python
from ata_coder.agent import CoderAgent
from ata_coder.config import AppConfig, AgentConfig

# 创建
config = AppConfig(agent=AgentConfig(workspace_dir="/project"))
agent = CoderAgent(config=config)

# 注册事件回调
agent.on_event(lambda event: handle(event))

# 运行任务
response = agent.run(
    task="Fix the bug in auth.py",
    stream=True,              # 启用流式输出
    skill_name=None,          # None = 自动检测
    explicit_model="",        # 空 = 自动路由
)
```

### 2.2 AgentEvent 类型

| 事件 | 字段 | 说明 |
|---|---|---|
| `TextDeltaEvent` | `text: str` | 流式文本增量 |
| `ToolCallEvent` | `tool_name, arguments, source` | 工具调用 |
| `ToolResultEvent` | `tool_name, result, arguments` | 工具结果 |
| `ReasoningEvent` | `text: str` | 模型推理/思考过程 |
| `ThinkingEvent` | — | 模型开始思考 |
| `SkillChangedEvent` | `skill_name: str` | 技能切换 |
| `ErrorEvent` | `error: str` | 错误 |
| `CompleteEvent` | `total_tool_calls, total_time` | 任务完成 |

### 2.3 事件回调

```python
def on_event(event: AgentEvent):
    if isinstance(event, TextDeltaEvent):
        print(event.text, end="")
    elif isinstance(event, ToolCallEvent):
        print(f"\n[TOOL] {event.tool_name}")
    elif isinstance(event, ErrorEvent):
        print(f"\n[ERROR] {event.error}")
    elif isinstance(event, CompleteEvent):
        print(f"\nDone: {event.total_tool_calls} tools, {event.total_time:.1f}s")

agent.on_event(on_event)
```

### 2.4 模型路由

```python
# 显式指定模型
agent.run(task="...", explicit_model="gpt-4o")

# 自动路由（AI 分类 → 简单/复杂 → 快/强模型）
agent.run(task="...")  # 自动选择模型

# 运行时切换
agent._route_model("gpt-4o-mini")
print(agent.current_model)
```

### 2.5 关键属性

| 属性 | 类型 | 说明 |
|---|---|---|
| `agent.config` | `AppConfig` | 完整配置 |
| `agent.tools` | `ToolExecutor` | 工具执行器 |
| `agent.skills` | `SkillManager \| None` | 技能管理器 |
| `agent.memory` | `MemoryStore \| None` | 记忆存储 |
| `agent.mcp` | `MCPClient \| None` | MCP 客户端 |
| `agent.change_tracker` | `ChangeTracker` | 文件修改追踪 |
| `agent.fool_proof` | `FoolProofEngine` | 安全防护引擎 |
| `agent.git` | `GitWorkflow` | Git 工作流 |
| `agent.session_id` | `str` | 当前会话 ID |
| `agent.current_model` | `str` | 当前使用的模型 |

---

## 3. Tool API（`tools.py`）

内置工具执行器，提供文件操作、Shell 命令、搜索等功能。

### 3.1 ToolExecutor

```python
from ata_coder.tools import ToolExecutor
from ata_coder.config import AgentConfig

executor = ToolExecutor(AgentConfig(workspace_dir="/project"))

# 执行工具
result = executor.execute("read_file", {"file_path": "main.py"})
if result.success:
    print(result.output)

# 注册编辑回调（用于 diff 显示）
executor.on_edit(lambda file_path, old_content: print(f"Editing {file_path}"))
```

### 3.2 ToolResult

| 属性/方法 | 类型 | 说明 |
|---|---|---|
| `success` | `bool` | 是否成功 |
| `output` | `str` | 输出内容 |
| `error` | `str` | 错误信息 |
| `to_message()` | `str` | 格式化为 LLM 消息 |
| `to_tool_result(call_id)` | `dict` | 格式化为 OpenAI tool result |

### 3.3 内置工具一览

| 工具名 | 说明 | 关键参数 |
|---|---|---|
| `read_file` | 读取文件（带行号、缓存） | `file_path`, `offset?`, `limit?` |
| `write_file` | 写入/创建文件（自动建目录） | `file_path`, `content` |
| `edit_file` | 精确字符串替换 | `file_path`, `old_string`, `new_string` |
| `run_shell` | 执行 Shell 命令 | `command`, `timeout?` |
| `grep` | 正则搜索文件内容 | `pattern`, `path?`, `glob?`, `case_sensitive?` |
| `glob` | 文件名模式匹配 | `pattern`, `path?` |
| `list_dir` | 列出目录内容 | `path?`, `recursive?` |
| `web_search` | DuckDuckGo 搜索 | `query`, `max_results?` |
| `web_fetch` | 抓取网页文本 | `url` |

### 3.4 添加自定义工具

```python
from ata_coder.tools import TOOL_DEFINITIONS

# 添加工具定义
TOOL_DEFINITIONS.append({
    "type": "function",
    "function": {
        "name": "my_tool",
        "description": "Does something useful",
        "parameters": {
            "type": "object",
            "properties": {
                "input": {"type": "string", "description": "..."},
            },
            "required": ["input"],
        },
    },
})

# 在 ToolExecutor 上实现（通过 monkey-patch 或子类化）
def _tool_my_tool(self, input: str) -> ToolResult:
    return ToolResult(success=True, output=f"Got: {input}")

ToolExecutor._tool_my_tool = _tool_my_tool
```

或使用扩展系统（推荐）：

```python
from ata_coder.extension import extension, Extension

@extension(name="my-tools", version="1.0.0", tags=["tool"])
class MyTools(Extension):
    def get_tools(self):
        return [{...}]  # 工具定义
```

### 3.5 文件缓存

`ToolExecutor` 维护文件读取缓存（"只读一遍"策略）：
- 首次读取文件后缓存内容 + mtime
- 再次读取同一文件（未修改）返回 `[cached]` 标记
- 使用 `offset`/`limit` 可以从缓存中提供特定片段
- `clear_file_cache()` 清空缓存

### 3.6 输出限制

| 常量 | 值 | 说明 |
|---|---|---|
| `MAX_READ_LINES` | 2000 | 无 limit 时默认最大行数 |
| `MAX_READ_CHARS` | 80000 | 读取输出硬上限（~20k tokens） |
| `MAX_OUTPUT_CHARS` | 100000 | 所有工具输出全局上限（~25k tokens） |
