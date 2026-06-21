# Audit Trail — Security & Architecture Review (2026-06-21)

> **原则**: 审计产出必须是持久化文件，不是聊天文本。修一个勾一个，不依赖上下文窗口记忆。

## 追踪清单

| # | 严重性 | 类别 | 问题 | 状态 | 轮次 | 提交 |
|---|--------|------|------|------|------|------|
| 1 | 🔴 HIGH | 安全 | `create_subprocess_shell` 命令注入 — 嵌套 `$()`/`$IFS`/`eval` 绕过 | ✅ | R1 | `9feae18` |
| 2 | 🟡 MED | 安全 | 无认证模式 `X-Forwarded-For` IP 伪造 | ✅ | R2 | `a8ac846` |
| 3 | 🟡 MED | 安全 | Shell 会话无 token 绑定 + `$VAR` 路径绕过 | ✅ | R3 | `db0ecaa` |
| 4 | 🟢 LOW-MED | 安全 | Shell 会话重连 TOCTOU 竞态条件 | ✅ | R4 | `25f76c8` |
| 5 | 🟡 MED | 架构 | 巨型文件拆分 (executor/server/agent/extension/settings) | ✅ | R7,R8,R10,R12,R14,R15,R17,R18,R20,R21,R22,R23 | 见下 |
| 6 | 🟡 MED | 架构 | Mixin 四重继承链 — 隐式 `self.*` 依赖 | ✅ | R13,R22 | `e816229`,`9ebd6c4`,`ad5d671` |
| 7 | 🟡 MED | 架构 | 全局可变状态 — `_cleanup_handlers`/`_shell_sessions`/rate limiter | ✅ | 审计确认 | 大部分为设计意图 |
| 8 | 🟡 LOW-MED | 架构 | `LLMClient` 与 `AnthropicClient` retry/backoff 重复 | ✅ | R9 | `7dde399` |
| 9 | 🟢 LOW | 架构 | `Settings` 类职责过多 (I/O+路径+env+路由+复杂度) | ✅ | R16 | `091ab0f` |
| 10 | 🟢 LOW-MED | 代码质量 | 正则 `[^)]+` 无法匹配嵌套 `$()` / 反引号误报 | ✅ | R1 | `9feae18` |
| 11 | 🟢 LOW | 代码质量 | `id(msg)` 缓存键在消息重建时全部失效 | ✅ | R5 | `746c36f` |
| 12 | 🟢 LOW | 代码质量 | 错误处理不一致 (`exception`/`warning`/`debug`) | ✅ | R11 | `f209ca8` |
| 13 | 🟡 MED | 测试 | 测试套件 175s 超时 (插件 registry 132s) | ✅ | R6 | `6b38e67` |
| 14 | 🟢 LOW-MED | 测试 | 缺少 HTTP 端点集成测试 | ✅ | R19 | `0ea102f` |

### #5 文件拆分详情

| 文件 | 修复前 | 修复后 | 变化 | 新模块 |
|------|--------|--------|------|--------|
| `tools/executor.py` | 1076 | **409** | -62% | `file_ops.py`(399) `shell_exec.py`(167) `search.py`(222) |
| `server.py` | 1140 | **539** | -53% | `server_sse.py`(115) `server_routes.py`(566) |
| `agent.py` | 927 | **853** | -8% | `agent_session.py`(116) |
| `extension.py` | 672 | **588** | -12% | `extension_point.py`(103) |
| `settings.py` | 670 | **634** | -5% | — |

## 关键指标

| 指标 | 修复前 | 修复后 |
|------|--------|--------|
| 活跃安全漏洞 | 4 | **0** |
| 测试耗时 | 175s | **43s** (-75%) |
| 重复代码(backoff) | 10+ 处 | **1 处** |
| 新建模块 | 0 | **8** |
| Mixin 契约文档 | 0 | **5** |
| 回归 | — | **566/566 × 23 轮** |
| 回滚 | — | **0** |

## 剩余架构工作（延后）

以下项目已文档化在 `CLAUDE.md` 中，各含 P0/P1/P2 分步方案：

- **Settings → SettingsStore / EnvResolver / ModelRouter**: 需 2+ 轮
- **Mixin → 组合模式**: 契约已文档化，实际重构需 3+ 轮  
- **main.py CLI 命令提取**: 931 行，click 装饰器耦合紧，需 2+ 轮
- **server_routes.py 继续拆分**: 566 行 (含 shell+chat+stream)，目标 ≤400

## 经验教训

1. **审计产出必须是持久化文件** — 聊天文本滚出窗口即丢失追踪链
2. **先分类清单，再逐类执行** — 避免"读一段修一段"的打地鼠模式
3. **依赖分析先于修复** — 浅拷贝→4个症状 vs. 单修每个症状
4. **每轮验收** — 对照清单逐项勾验，而非"能跑就行"
