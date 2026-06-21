# Contributing to ATA Coder

Welcome, contributor! 👋 This handbook covers everything you need to go from `git clone` to merged PR. Read it before your first commit — it'll save you time and review rounds.

---

## Table of Contents

- [Philosophy](#philosophy)
- [Quick Setup](#quick-setup)
- [Architecture Overview](#architecture-overview)
- [Development Workflow](#development-workflow)
- [The Iron Rules](#the-iron-rules)
- [Commit Format](#commit-format)
- [Testing](#testing)
- [Code Review Checklist](#code-review-checklist)
- [Adding a Skill](#adding-a-skill)
- [Adding a Tool](#adding-a-tool)
- [Release Process](#release-process)
- [Getting Help](#getting-help)

---

## Philosophy

ATA Coder is built on three principles:

1. **Keep it small.** Every module should fit in one sitting. Files over 500 lines are a bug report waiting to happen.
2. **One thing at a time.** A PR fixes a bug, adds a feature, or refactors — never two at once.
3. **Trust the pipeline.** Safety checks, permissions, privilege escalation — each layer does exactly one job and delegates the rest.

We optimize for maintainability over cleverness. The code should read like the person who wrote it actually wanted you to understand it.

---

## Quick Setup

```bash
# Clone
git clone https://github.com/jiaheng0815/ata-coder.git
cd ata-coder

# Virtual environment
python -m venv .venv
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate         # Windows

# Install with dev dependencies
pip install -e ".[dev]"

# Run the test suite
pytest tests/ --ignore=tests/test_server.py -q
```

You're looking for **566 tests, all green**. On Windows, `test_server.py` is skipped by design (stdlib `HTTPServer.handle_request()` blocks indefinitely on Windows).

### Recommended tooling

| Tool | Purpose |
|------|---------|
| `ruff` | Linting + formatting (`ruff check . && ruff format .`) |
| `mypy` | Type checking (`mypy ata_coder/`) |
| `pytest` | Test runner (configured in `pyproject.toml`) |

All three are pre-configured in `pyproject.toml`. No additional setup needed.

---

## Architecture Overview

Before you touch code, understand the shape of the system.

### The Big Picture

```
User Input (CLI / HTTP / REPL)
        │
        ▼
AgentController (asyncio.Task)     ← lifecycle orchestrator
        │
        ▼
CoderAgent                          ← core agent: LLM loop + tool dispatch
   ├── ToolExecutionMixin           ← executes tools, streams output
   ├── CompactionMixin              ← token tracking + context compaction
   ├── ModelRoutingMixin            ← task classification + model selection
   └── ExtensionMixin               ← skill/extension lifecycle
        │
        ├──→ LLM Clients (httpx.AsyncClient)
        │       ├── LLMClient          ← OpenAI-compatible API
        │       └── AnthropicClient    ← Anthropic Messages API
        │
        ├──→ ToolExecutor             ← 14 tool handlers
        │       ├── file ops (read/write/edit/rename)
        │       ├── shell execution
        │       ├── search (grep/glob/web)
        │       ├── sub-agents
        │       └── MCP
        │
        ├──→ Safety Pipeline
        │       SafetyGuard → FoolProof → Permissions → Privilege
        │
        └──→ Event Bus (asyncio.Queue)
                → REPL (prompt_toolkit + Rich)
                → HTTP Server (SSE streaming)
```

### Key modules for newcomers

| Module | Why you'll touch it |
|--------|-------------------|
| `agent.py` | Core agent loop, event emission, session persistence |
| `tools/executor.py` | Adding or modifying tool behavior |
| `tools/definitions.py` | Adding a new tool — define its schema here |
| `skills.py` | Skill auto-detection, activation, loading |
| `llm_client.py` | API communication, retry logic, streaming |
| `config.py` | Configuration resolution (reads `settings.json`) |
| `repl_ui.py` | Terminal UI — event handling, diff rendering |
| `server.py` | HTTP API server, SSE, routing |

### Design patterns

- **Async everywhere.** Everything is `asyncio` — no threads, no `concurrent.futures`, no callbacks.
- **Mixin composition.** `CoderAgent` inherits from four mixins. Each mixin has a clear contract. Don't add cross-mixin dependencies.
- **Event-driven.** Agent emits `@dataclass` events to an `asyncio.Queue`. Both REPL and HTTP server consume from the same queue.
- **Config flow.** `settings.json` → `Settings` → `AppConfig`/`LLMConfig`. No module reads `os.environ` directly.
- **Atomic writes.** `memory.py` and `session.py` write to `.tmp` then `os.replace()`. Follow this pattern for any new persistent writes.

### Safety pipeline order

```
1. SafetyGuard    — pattern-based risk analysis (CRITICAL/DANGER/CAUTION/SAFE)
2. FoolProof      — unified pre-execution check + dry-run preview
3. Permissions    — interactive allow/deny/ask per tool category
4. Privilege      — OS-aware elevation (sudo/pkexec/Start-Process)
```

Every tool execution flows through all four layers. Don't skip layers — each one catches things the previous couldn't.

---

## Development Workflow

### 1. Pick up work

Find or create an issue. Comment to say you're working on it. If you're fixing something not tracked in issues, create one first — it gives us a place to discuss scope before you write code.

### 2. Branch

```bash
git checkout -b fix/your-description        # Bug fixes
git checkout -b feat/your-description       # New features
git checkout -b refactor/your-description   # Refactoring
```

Branch from `master`. Keep branches short-lived — a branch that lives more than a few days is a branch that will have merge conflicts.

### 3. Write code

Follow the [Iron Rules](#the-iron-rules). Run tests locally before pushing:

```bash
pytest tests/ --ignore=tests/test_server.py -q
ruff check ata_coder/
```

### 4. Commit

Follow the [Commit Format](#commit-format). Each commit should be a logical unit — if you can't describe it in one sentence, split it.

### 5. Push and open a PR

Push to your fork, open a PR against `master`. The PR description should answer:

- **What** does this change?
- **Why** is it needed?
- **How** was it tested?
- **What's the rollback plan** if it breaks?

### 6. Review

A maintainer will review. Expect feedback on:
- Rule compliance (files changed, lines added, complexity)
- Test coverage
- Error handling conventions
- Commit message format

Address feedback in new commits — we squash-merge, so clean history inside the branch isn't required.

---

## The Iron Rules

> ⚠️ **These rules are non-negotiable.** A PR that violates any of them will be returned without review.

### Core Development Rules

1. **One problem per change.** A single PR fixes one bug, adds one feature, or performs one refactor. Never mix. If you spot a formatting issue while fixing a bug — open a separate PR.

2. **No defensive coding.** Don't add null-checks, try/except blocks, or fallback paths for scenarios you haven't reproduced. Handle what actually happens; let the global error handler catch the rest.

3. **Never delete comments.** If a comment is outdated, append a correction below it. Never remove or overwrite existing comments. This preserves institutional knowledge and blame history.

### Hard Limits

| Metric | Limit | Enforcement |
|--------|-------|-------------|
| Files changed per PR | ≤ 3 | Reviewer checks |
| Lines added + deleted | ≤ 200 (including tests) | Reviewer checks |
| McCabe cyclomatic complexity (new functions) | ≤ 10 | Manual review |
| New third-party dependencies | 0 (without explicit approval) | Blocked at review |
| Logging | `logger.error()` / `logger.warning()` only — never `print()` | Linted by convention |
| Residue | No `TODO`, `FIXME`, or hardcoded IPs/domains in final code | Grep before merge |

### Self-Check Before Push

Run through this checklist before you open a PR:

- [ ] Did I **only change logic**, without adjusting unrelated indentation or blank lines?
- [ ] Did I avoid `os.system()`, `subprocess.call()`, `eval()`, or other高危 functions?
- [ ] If I modified async code, did I verify no new deadlocks or race conditions?
- [ ] Do my new functions have a single clear responsibility?
- [ ] Did I run `pytest` and `ruff` locally?

---

## Commit Format

Every commit message must follow this three-part structure:

```
<type>: [problem] -> [expected behavior]

回滚方案：若合并后出现异常，请执行 git revert HEAD 无损回退。

变更列表：
- file.py: function_name — brief description
- file2.py: ClassName.method — brief description
```

### Type prefixes

| Prefix | When to use |
|--------|------------|
| `fix:` | Bug fix |
| `feat:` | New feature |
| `refactor:` | Code restructuring (no behavior change) |
| `docs:` | Documentation only |
| `test:` | Test additions or fixes |
| `chore:` | Maintenance (deps, config, build) |

### Examples

Good:
```
fix: subprocess shell hang on Windows -> stream pipes instead of communicate()

回滚方案：若合并后出现异常，请执行 git revert HEAD 无损回退。

变更列表：
- tools/executor.py: _tool_run_shell — replace communicate() with async pipe streaming
```

Bad:
```
fix bug
```

---

## Testing

### Running tests

```bash
pytest                                              # Full suite
pytest tests/ --ignore=tests/test_server.py          # Windows-safe
pytest tests/test_tools.py -q                        # Single file
pytest -k "agent" -q                                 # Filter by name
pytest -m "not slow"                                 # Skip slow tests
```

### Test conventions

- **File naming:** `test_<module>.py` in `tests/`
- **Function naming:** `test_<what_it_tests>`
- **Async tests:** Use `pytest-asyncio` with `asyncio_mode = "auto"` (pre-configured)
- **Slow tests:** Mark with `@pytest.mark.slow` (and `@pytest.mark.integration` if they need network)
- **Server tests:** Mark with `@pytest.mark.server`

### Writing new tests

1. Put tests in `tests/` — not inside the package
2. Cover the happy path *and* at least one error path
3. For tool tests, verify both `ToolResult.success` and the output content
4. Mock external dependencies (network, subprocess) — don't make real API calls in unit tests
5. Use `pytest.raises()` for expected exceptions; don't catch them yourself

### What we test

| Layer | What | Where |
|-------|------|-------|
| **Tools** | Every tool handler — valid input, invalid input, edge cases | `tests/test_tools.py` |
| **Agent** | LLM loop, event emission, session persistence | `tests/test_agent.py` |
| **Config** | Settings resolution, env fallback, defaults | `tests/test_config.py` |
| **Safety** | Risk patterns, path traversal, permission rules | `tests/test_safety.py` |
| **Server** | HTTP endpoints, SSE streaming, session CRUD | `tests/test_server.py` |
| **Skills** | Skill loading, activation, prompt aggregation | `tests/test_skills.py` |
| **Integration** | End-to-end flows (marked `@pytest.mark.integration`) | Various |

---

## Code Review Checklist

When reviewing someone else's PR (or your own), verify:

### Mechanical
- [ ] ≤ 3 files changed
- [ ] ≤ 200 lines added + deleted
- [ ] No new dependencies
- [ ] No `TODO`/`FIXME`/hardcoded IPs
- [ ] Commit message follows the three-part format

### Logic
- [ ] Change does exactly one thing
- [ ] No unrelated formatting changes
- [ ] No defensive null-checks for unreproduced scenarios
- [ ] Existing comments preserved (new corrections appended)

### Safety
- [ ] New shell commands go through the safety pipeline
- [ ] File writes are atomic (tmp + rename)
- [ ] No `eval()`, `os.system()`, or `subprocess.call()`
- [ ] Async code doesn't introduce new race conditions

### Quality
- [ ] New functions have cyclomatic complexity ≤ 10
- [ ] Error handling follows conventions (`logger.exception()` for internal, `logger.warning()` for external)
- [ ] Exception chaining: `raise NewError(...) from e`
- [ ] Tests cover the change

---

## Adding a Skill

Skills are the recommended way to extend ATA Coder. They're self-contained folders with a manifest and optional Python handlers.

### Structure

```
ata_coder/skills/<skill-name>/
├── SKILL.md           # Required: YAML frontmatter manifest
├── handler.py         # Optional: Python entry point
├── utils.py           # Optional: Helpers
├── prompts/           # Optional: LLM prompt templates (.md)
├── resources/         # Optional: Static data files
└── tests/             # Recommended: pytest tests
```

### SKILL.md manifest

```markdown
---
name: my-skill
version: "1.0"
description: What this skill does
triggers:
  - keyword1
  - keyword2
tools:
  - read_file
  - write_file
  - run_shell
---
# My Skill

## What it does

Brief description of the skill's purpose and behavior.
```

### Key rules

1. **Triggers** are used for keyword-based auto-detection. Keep them specific — avoid generic words like "fix" or "help".
2. **Tools** list restricts which tools the skill can use. If omitted, all tools are available. The intersection of all active skills' tool lists is used.
3. **Prompts** in `prompts/` use `{% if condition %}` Jinja-like templating.
4. **Handler** in `handler.py` should expose an `async def run(agent, task, **kwargs)` function.

### After adding a skill

1. Add tests in the skill's `tests/` folder
2. Run the full test suite to make sure you haven't broken skill auto-detection
3. Document any new dependencies the skill requires

---

## Adding a Tool

Tools are the primitive operations the agent can perform. Adding one requires touching both the schema definition and the executor.

### 1. Define the tool schema

In `ata_coder/tools/definitions.py`, add to `TOOL_DEFINITIONS`:

```python
{
    "type": "function",
    "function": {
        "name": "my_new_tool",
        "description": "What this tool does, when to use it, what it returns.",
        "parameters": {
            "type": "object",
            "properties": {
                "param1": {
                    "type": "string",
                    "description": "Description of param1"
                }
            },
            "required": ["param1"]
        }
    }
}
```

### 2. Implement the handler

In `ata_coder/tools/executor.py`, add a `_tool_my_new_tool` method:

```python
async def _tool_my_new_tool(self, param1: str) -> ToolResult:
    """What this tool does."""
    try:
        # Implementation
        result = await do_something(param1)
        return ToolResult(success=True, output=result)
    except Exception as e:
        logger.warning("my_new_tool failed for %s: %s", param1, e)
        return ToolResult(success=False, output="", error=str(e))
```

### 3. Register in the dispatch table

In `ToolExecutor.__init__`, add to the `_tool_handlers` dict:

```python
self._tool_handlers = {
    # ... existing handlers ...
    "my_new_tool": self._tool_my_new_tool,
}
```

### 4. Wire up safety

Ensure any new side-effect paths go through the safety pipeline (`fool_proof.evaluate()` before execution).

### 5. Test

Add tests in `tests/test_tools.py`:
- Valid input produces correct `ToolResult`
- Invalid input returns `success=False`
- Edge cases (empty input, extreme values, concurrent calls)

---

## Release Process

Releases follow a strict checklist. Every version bump **must** ship to both GitHub Releases and PyPI.

```bash
# 1. Bump version in: main.py, pyproject.toml, setup_wizard.py
# 2. Update README + CHANGELOG with the new version section
# 3. Run tests
pytest tests/ -q

# 4. Commit + push
git add -A && git commit -m "release: vX.Y.Z" && git push

# 5. Build
python -m build --sdist --wheel

# 6. Upload to PyPI
twine upload --username __token__ --password "$PYPI_TOKEN" dist/ata_coder-X.Y.Z-*

# 7. Create GitHub release + upload artifacts
gh release create vX.Y.Z --title "vX.Y.Z — <summary>" --notes "<release notes>"
gh release upload vX.Y.Z dist/ata_coder-X.Y.Z-py3-none-any.whl dist/ata_coder-X.Y.Z.tar.gz
```

⚠️ **Never commit a version bump without completing both uploads.** PyPI token is read from the `PYPI_TOKEN` environment variable — never hardcoded.

For contributors: you don't need to do this. Maintainers handle releases. But your PR's changelog entry in the PR description helps us write the release notes.

---

## Getting Help

- **Bug reports & feature requests:** [GitHub Issues](https://github.com/jiaheng0815/ata-coder/issues)
- **Questions about the codebase:** Open a Discussion or comment on the relevant issue
- **PR feedback:** Tag a maintainer in your PR if it hasn't been reviewed within a few days

We're building this together. Good PRs get merged fast — and bad ones get helpful feedback. Don't be shy. 🚀
