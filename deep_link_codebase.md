# Deep Linking Implementation — ATA Coder Codebase Trace

> **Workspace:** `D:\code\ata_coder`  
> **Project:** ATA Coder v2.5.5 — AI Coding Assistant (Python 3.10+, MIT)  
> **Trace Date:** 2025-07-18

---

## Executive Summary

ATA Coder implements **five distinct deep-linking subsystems** that enable navigation between discrete pieces of information — memory entries, code locations, session records, change history, and external resources. The linking mechanisms span the full stack: Python core, TypeScript server, web UI, and system prompts.

---

## 1. Memory Wiki-Style Cross-Links (`[[...]]`)

### Location
- **Definition:** `memory.py:677-679` — `MemoryStore._extract_links()`
- **Rendering:** `memory.py:672-674` — emitted into the system prompt as `Related: link1, link2`
- **Example file:** `memory/user-prefs.md:10` — `[[coding-preferences]]`
- **Tests:** `tests/test_memory.py:491-498` — `test_extract_links()`

### Mechanism
```
┌──────────────────────────────────────────────────────┐
│  Memory content: "See [[python-tips]] for details"   │
│                         │                            │
│  _extract_links() ──────┘                            │
│  → regex: r'\[\[([^\]]+)\]\]'                       │
│  → returns: ["python-tips"]                          │
│                         │                            │
│  recall_context() ──────┘                            │
│  → outputs: "Related: python-tips"                   │
│  → injected into LLM system prompt                   │
└──────────────────────────────────────────────────────┘
```

### Key Code
```python
# memory.py:677-679
def _extract_links(self, content: str) -> list[str]:
    """Extract [[wiki-style]] links from content."""
    return re.findall(r"\[\[([^\]]+)\]\]", content)
```

### Resolution Strategy
Wiki links are **not automatically resolved** to their target content. They serve as **semantic hints**:
1. The link text (e.g., `python-tips`) matches a memory `name` (filename slug)
2. The `recall_context()` method appends `Related: <links>` to the prompt
3. The LLM can then reference or search for those linked memories
4. The memory store's TF-IDF search (`memory.py:467`) uses token overlap, not link graph traversal

---

## 2. File Path + Line Number References (`file_path:line_number`)

### Location
- **Prompt convention:** `prompts/output-style.md:19` — instructs the LLM to use this format
- **Skill guidance:** `skills/general-coder/SKILL.md:45` — reinforces clickable references
- **RAG output:** `rag_memory.py:462-464` — formats chunk references as headers
- **Chunk ID hashing:** `rag_memory.py:325-327` — `sha256("file_path:line")[:16]`
- **Workflow display:** `commands/_workflow.py:775` — `file_path:start_line`

### Mechanism
```
┌─────────────────────────────────────────────────────────────┐
│  System prompt convention (prompts/output-style.md:19):     │
│  "include `file_path:line_number` to allow easy navigation" │
│                                                             │
│  LLM output example:                                        │
│  "The bug is at agent.py:272 — the route is misconfigured"  │
│                         │                                   │
│  RAG search output (rag_memory.py:462-464):                 │
│  ### [1] memory.py:677-679 (function: _extract_links)       │
│                         │                                   │
│  Web UI (web/ts/app.ts:136-171):                            │
│  renderMarkdown() — plain text display, no auto-linkify     │
└─────────────────────────────────────────────────────────────┘
```

### Key Code — RAG Chunk Hashing
```python
# rag_memory.py:324-327
@staticmethod
def _hash_id(file_path: str, line: int) -> str:
    raw = f"{file_path}:{line}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
```

### Key Code — RAG Context Formatting
```python
# rag_memory.py:462-464
header = (
    f"\n### [{i}] {c.file_path}:{c.start_line}-{c.end_line} "
    f"({c.kind}: {c.name}) [score: {sr.score:.2f}]"
)
```

### Resolution
These are **display-only references** — the web UI does not currently hyperlink `file_path:line` patterns. The CLI terminal may support clickable paths via terminal emulator features (e.g., iTerm2, Windows Terminal semantic tokens). The LLM is instructed to produce this format so that IDE-integrated terminals can make them clickable.

---

## 3. Memory Index Markdown Links

### Location
- **Index format:** `memory.py:370` — `f"- [{memory.description}]({memory.file_path})"`
- **Index file:** `memory/MEMORY.md` — the persistent index of all memories
- **Resolution:** `memory.py:372-376` — updates existing entries by matching `](file_path)`

### Mechanism
```
MEMORY.md (auto-generated index):
┌──────────────────────────────────────────────────┐
│ - [User coding preferences](coding-preferences.md)│
│ - [Project architecture](project-arch.md)         │
│ - [API reference](api-reference.md)               │
└──────────────────────────────────────────────────┘
```

These are **standard Markdown links** (`[text](relative-path.md)`) within the memory directory. They enable:
- GitHub/GitLab rendering of the MEMORY.md as a navigable wiki
- VS Code's built-in Markdown preview with Ctrl+Click navigation
- Any Markdown viewer to browse the memory hierarchy

---

## 4. Session ID Deep Linking (REST API)

### Location
- **Server routes:** `server.py:293-295` (list), `server.py:303-305` (get by ID), `server.py:331-333` (delete)
- **Session store:** `server_session.py` — thread-safe session management
- **TypeScript server:** `ts-server/src/server.ts:143-146`, `ts-server/src/session-store.ts`
- **CLI resume:** `main.py:280-283` — `ata --resume <session-id>`
- **Session ID generation:** `session.py` — `generate_session_id()` with hash suffix

### Mechanism
```
Server REST API:
  GET    /sessions           → List all active sessions
  GET    /sessions/<id>      → Get specific session info (deep link)
  DELETE /sessions/<id>      → Delete a session

CLI Resume:
  ata --resume <hash-suffix> → Resume a saved session by 8-char hash

Session ID format:
  <task-slug>-<8-char-hash>
  e.g., "add-type-hints-a3f8b91c"
```

### Key Code — Server Route Dispatch
```python
# server.py:303-305
elif len(parts) == 2 and parts[0] == "sessions":
    if not self._require_auth("sessions"): return
    self._handle_get_session(parts[1])
```

### Key Code — Session ID Resolution
```python
# session.py (from CHANGELOG.md:408)
# Resume by hash: ata --resume a3f8b91c
# SessionManager.resolve_session_id() finds the full session by suffix match
```

---

## 5. Change Tracker ID References (Undo/Redo Stack)

### Location
- **Core:** `change_tracker.py:96-249` — `ChangeTracker` class
- **Change IDs:** Each change gets a sequential `id` (`change_tracker.py:149-150`)
- **Display:** `change_tracker.py:83-89` — summaries with `#id` prefix
- **Backup path:** `.ata_coder/changes/<session-id>/`

### Mechanism
```
Change tracking creates a navigable undo stack:
  #1 CREATE  src/new_file.py
  #2 EDIT    agent.py (320→325 lines)
  #3 DELETE  temp.log

Commands:
  /undo <n>     → Revert last N changes (by index)
  /restore <n>  → Re-apply reverted change (by ID)
  /changes      → List all changes (ID + file_path + diff)
```

### Key Code
```python
# change_tracker.py:79-89
@property
def summary(self) -> str:
    """One-line summary."""
    status = "[REVERTED]" if self.reverted else ""
    if self.change_type == ChangeType.WRITE:
        return f"#{self.id} CREATE {self.file_path} {status}"
    elif self.change_type == ChangeType.EDIT:
        old_lines = (self.old_content or "").count("\n") + 1 if self.old_content else 0
        new_lines = (self.new_content or "").count("\n") + 1 if self.new_content else 0
        return f"#{self.id} EDIT   {self.file_path} ({old_lines}→{new_lines} lines) {status}"
```

---

## 6. Clawd Desktop Pet Integration (External Session Linking)

### Location
- **Integration:** `clawd_integration.py` — bridges to Clawd desktop pet
- **Session linking:** `clawd_integration.py:138` — `start(session_id=..., cwd=...)`
- **Permission bubbles:** `clawd_integration.py:10-12` — delegates Y/N/A/D to Clawd UI

### Mechanism
```
ATA Coder ←→ Clawd Desktop Pet (HTTP on 127.0.0.1:23333-23337)
  - Session start/end events carry session_id
  - Permission decisions deep-link back to ATA's tool execution
  - Runtime detection via ~/.clawd/runtime.json
```

---

## 7. Project Identity Deep Linking

### Location
- **Detection:** `memory_project.py:36-55` — `detect_project_id()`
- **Identity:** `memory_project.py:54-55` — `sha256(git_remote_url or cwd)[:12]`
- **Scoping:** Memories tagged with `project_id` for workspace-specific recall

### Mechanism
```
Project ID = sha256(git_remote_url || workspace_path)[:12]

Enables:
  - Project-scoped memory recall (memory_project.py)
  - Session checkpoint restore per project
  - Cross-session continuity within the same git repository
```

---

## 8. Web UI Link Rendering

### Location
- **TypeScript:** `web/ts/app.ts:136-171` — `renderMarkdown()`
- **JavaScript (compiled):** `web/js/app.js:71-91`
- **CSS:** `web/css/style.css`

### Current State
The web UI's `renderMarkdown()` function handles:
- Fenced code blocks → `<pre><code>`
- Headings → `<h1>/<h2>/<h3>`
- Inline code, bold, italic
- Paragraph breaks

It does **NOT** currently auto-linkify:
- `file_path:line_number` patterns
- `[[wiki-style]]` references
- Memory index `[text](file.md)` links
- URLs (plain text only)

---

## Architecture Summary

```
┌──────────────────────────────────────────────────────────────┐
│                    DEEP LINKING LAYERS                        │
├────────────────┬─────────────────┬───────────────────────────┤
│  Memory Layer  │  Code Layer     │  Session Layer             │
│                │                 │                            │
│  [[wiki]]      │  file:line      │  /sessions/<id>            │
│  [text](.md)   │  chunk hashes   │  --resume <hash>           │
│  MEMORY.md     │  RAG refs       │  clawd session_id          │
│                │  change #ids    │  project_id                │
├────────────────┴─────────────────┴───────────────────────────┤
│  Resolution:  LLM prompt hints  |  Terminal click  |  REST   │
│  Display:     Text only (no hyperlink auto-generation)       │
└──────────────────────────────────────────────────────────────┘
```

---

## Key Files Reference

| File | Role |
|------|------|
| `memory.py:677-679` | Wiki-style link extraction |
| `memory.py:370-378` | Memory index with Markdown links |
| `rag_memory.py:325-327` | File:line chunk ID hashing |
| `rag_memory.py:462-464` | RAG result formatting with file:line refs |
| `change_tracker.py:79-89` | Change ID + file_path summaries |
| `server.py:303-305` | REST deep link: GET /sessions/\<id\> |
| `server.py:331-333` | REST deep link: DELETE /sessions/\<id\> |
| `session.py` | Session ID generation + resolution |
| `server_session.py` | Session store with TTL eviction |
| `clawd_integration.py:138` | External session linking to Clawd |
| `memory_project.py:36-55` | Project identity via git remote |
| `prompts/output-style.md:19` | LLM instruction for file:line format |
| `skills/general-coder/SKILL.md:45` | Skill-level deep link convention |
| `web/ts/app.ts:136-171` | Web UI markdown renderer |
| `ts-server/src/server.ts:143-146` | TS server session routes |
| `ts-server/src/session-store.ts` | TS session CRUD |
| `tools/definitions.py` | Tool parameter definitions (file_path, etc.) |
| `tools/executor.py:324` | File cache reference display |
