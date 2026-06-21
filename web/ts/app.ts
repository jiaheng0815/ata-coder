/**
 * ATA Coder Web UI — TypeScript Application
 *
 * Connects to the `ata server` backend via REST + SSE streaming.
 * Zero framework dependencies — vanilla TS + fetch ReadableStream.
 */

// ── Constants ────────────────────────────────────────────────────────────────────

const TOKEN_ESTIMATE_PER_TOOL = 200;
const ARG_STRING_MAX_LEN = 60;
const TOOL_OUTPUT_MAX_LEN = 150;

// ── Types ────────────────────────────────────────────────────────────────────────

interface Message {
  role: 'user' | 'agent' | 'think' | 'tool' | 'tool-ok' | 'tool-err' | 'error' | 'status' | 'sep';
  text: string;
  id?: string;
}

interface AppState {
  sessionId: string;
  model: string;
  skill: string;
  workspace: string;
  tokens: number;
  thinking: boolean;
  streaming: boolean;
  startTime: number;
  commands: Array<[string, string]>;
}

interface SSEEvent {
  type: 'text' | 'thinking' | 'tool_call' | 'tool_result' | 'error' | 'complete' | 'done';
  text?: string;
  tool?: string;
  args?: Record<string, unknown>;
  ok?: boolean;
  output?: string;
  error?: string;
  tools?: number;
  time?: number;
  session_id?: string;
  response?: string;
}

// ── State ────────────────────────────────────────────────────────────────────────

const state: AppState = {
  sessionId: '',
  model: '…',
  skill: 'general-coder',
  workspace: '…',
  tokens: 0,
  thinking: false,
  streaming: false,
  startTime: 0,
  commands: [],
};

const el = {
  chat: document.getElementById('chat')!,
  input: document.getElementById('cmd-input') as HTMLInputElement,
  sendBtn: document.getElementById('send-btn')!,
  stopBtn: document.getElementById('stop-btn')!,
  popup: document.getElementById('cmd-popup')!,
  dot: document.getElementById('status-dot')!,
  subtitle: document.getElementById('top-subtitle')!,
  initInfo: document.getElementById('init-info')!,
  modelSelect: document.getElementById('model-select') as HTMLSelectElement,
  skillTags: document.getElementById('skill-tags')!,
  wsPath: document.getElementById('ws-path')!,
  stTokens: document.getElementById('st-tokens')!,
  stSkill: document.getElementById('st-skill')!,
  stModel: document.getElementById('st-model')!,
  stTime: document.getElementById('st-time')!,
};

let popupIdx = -1;
let activeReq: AbortController | null = null;

// ── Helpers ──────────────────────────────────────────────────────────────────────

function esc(s: string): string {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function fmtTime(s: number): string { return s.toFixed(1) + 's'; }

// ── UI Rendering ─────────────────────────────────────────────────────────────────

function appendBubble(msg: Message): void {
  const div = document.createElement('div');
  div.className = `msg ${msg.role}`;
  div.innerHTML = renderMarkdown(msg.text);
  el.chat.appendChild(div);
  scrollBottom();
}

function appendStreamBubble(text: string, cls: string): HTMLElement {
  let div = el.chat.lastElementChild as HTMLElement | null;
  if (!div || !div.classList.contains(cls) || !div.classList.contains('streaming')) {
    div = document.createElement('div');
    div.className = `msg ${cls} streaming`;
    div.dataset.raw = text;
    div.innerHTML = renderMarkdown(text);
    el.chat.appendChild(div);
  } else {
    div.dataset.raw = (div.dataset.raw || '') + text;
    div.innerHTML = renderMarkdown(div.dataset.raw);
  }
  scrollBottom();
  return div;
}

function flushStreamBubbles(): void {
  el.chat.querySelectorAll('.streaming').forEach(d => {
    const el = d as HTMLElement;
    el.classList.remove('streaming');
    delete el.dataset.raw;
  });
}

/**
 * Simple regex-based Markdown renderer.
 * Pipeline: extract code blocks → headings → newlines → paragraph wrap → inline format → restore blocks.
 *  1. Fenced code blocks are preserved (newlines inside &lt;pre&gt; render natively).
 *  2. ### / ## / # at line start → &lt;h3&gt; / &lt;h2&gt; / &lt;h1&gt; (bold via CSS).
 *  3. Double \n\n → paragraph break.
 *  4. Single \n → line break.
 *  5. Inline code, bold, italic applied last (won't match inside code blocks).
 */
function renderMarkdown(text: string): string {
  // Step 1 — extract fenced code blocks to protect their content
  const codeBlocks: string[] = [];
  let html = esc(text);
  html = html.replace(
    /```(\w*)\n([\s\S]*?)```/g,
    (_: string, lang: string, code: string) => {
      const idx = codeBlocks.length;
      codeBlocks.push(
        `<pre><code class="${esc(lang)}">${esc(code.trimEnd())}</code></pre>`
      );
      return `\x00CB${idx}\x00`;
    }
  );

  // Step 2 — headings (### before ## before # — longest match first)
  html = html.replace(/(^|\n)### (.+?)(?=\n|$)/gm, '$1</p><h3>$2</h3><p>');
  html = html.replace(/(^|\n)## (.+?)(?=\n|$)/gm, '$1</p><h2>$2</h2><p>');
  html = html.replace(/(^|\n)# (.+?)(?=\n|$)/gm, '$1</p><h1>$2</h1><p>');

  // Step 3 — convert newlines to HTML (paragraphs + line breaks)
  html = html.replace(/\n\n/g, '</p><p>');
  html = html.replace(/\n/g, '<br>');
  html = `<p>${html}</p>`;
  html = html.replace(/<p><\/p>/g, '');           // clean up empty paragraphs

  // Step 3 — inline formatting (won't touch code blocks — they're placeholders now)
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');

  // Step 4 — restore code blocks
  html = html.replace(/\x00CB(\d+)\x00/g, (_m, idx) => codeBlocks[parseInt(idx)]);

  return html;
}

function scrollBottom(): void {
  requestAnimationFrame(() => {
    el.chat.scrollTop = el.chat.scrollHeight;
  });
}

function setThinking(on: boolean): void {
  state.thinking = on;
  el.dot.className = `dot ${on ? 'thinking' : 'ready'}`;
  el.subtitle.textContent = on ? 'Thinking…' : '';
}

function updateStatus(): void {
  el.stTokens.textContent = `tokens: ~${state.tokens.toLocaleString()}`;
  el.stSkill.textContent = `skill: ${state.skill}`;
  el.stModel.textContent = `model: ${state.model}`;
  if (state.startTime > 0) {
    el.stTime.textContent = fmtTime((Date.now() - state.startTime) / 1000);
  }
}

/**
 * Toggle streaming UI state via CSS class on <body>.
 * CSS handles button visibility + input disabled appearance.
 * We ALSO toggle el.input.disabled so the browser truly blocks typing.
 */
function setStreaming(on: boolean): void {
  state.streaming = on;
  document.body.classList.toggle('streaming', on);
  el.input.disabled = on;
}

// ── Safe focus: re-enabling a disabled input needs a frame before focus ──────────

function safeFocusInput(): void {
  requestAnimationFrame(() => {
    el.input.disabled = false;  // belt-and-suspenders with setStreaming(false)
    el.input.focus();
  });
}

// ── API Calls ────────────────────────────────────────────────────────────────────

async function apiGet(path: string): Promise<unknown> {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

async function apiPost(path: string, body: Record<string, unknown>): Promise<unknown> {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
    throw new Error(err.error || `HTTP ${r.status}`);
  }
  return r.json();
}

async function fetchInit(): Promise<void> {
  try {
    const [health, skills, models] = await Promise.all([
      apiGet('/health') as Promise<{ model: string; workspace: string; skills: string[] }>,
      apiGet('/skills') as Promise<{ skills: Array<{ name: string }> }>,
      apiGet('/models') as Promise<{ models: Array<{ id: string }>; current: string }>,
    ]);
    state.model = health.model;
    state.workspace = health.workspace;
    el.modelSelect.innerHTML = (models.models || []).map(m =>
      `<option ${m.id === models.current ? 'selected' : ''}>${m.id}</option>`
    ).join('');
    if ((models.models || []).length === 0) {
      el.modelSelect.innerHTML = `<option>${state.model}</option>`;
    }
    el.skillTags.innerHTML = (skills.skills || []).map(s =>
      `<span class="skill-tag">${esc(s.name)}</span>`
    ).join('');
    el.wsPath.textContent = state.workspace;
    el.initInfo.textContent = `Model: ${state.model}  ·  Workspace: ${state.workspace}`;
    el.stModel.textContent = `model: ${state.model}`;
    updateStatus();
  } catch {
    el.initInfo.textContent = '⚠ Server unreachable — start with: ata gui';
  }
}

// ── Chat Send ────────────────────────────────────────────────────────────────────

async function sendChat(message: string): Promise<void> {
  if (state.streaming || !message.trim()) return;

  appendBubble({ role: 'user', text: message });
  el.input.value = '';
  hidePopup();
  flushStreamBubbles();
  state.startTime = Date.now();

  // ── Enter streaming mode ──
  setStreaming(true);
  setThinking(true);

  try {
    activeReq = new AbortController();
    const resp = await fetch('/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message,
        session_id: state.sessionId || undefined,
        skill: state.skill,
        model: state.model,
      }),
      signal: activeReq.signal,
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ error: `HTTP ${resp.status}` }));
      throw new Error(err.error || `HTTP ${resp.status}`);
    }

    const reader = resp.body?.getReader();
    if (!reader) throw new Error('No response body');

    const decoder = new TextDecoder();
    let buffer = '';
    let currentEvent = '';
    let toolCount = 0;
    let totalTime = 0;
    let firstContent = false;
    let streamDone = false;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        if (line.startsWith('event: ')) {
          currentEvent = line.slice(7).trim();
          continue;
        }
        if (!line.startsWith('data: ')) continue;

        try {
          const data: SSEEvent = JSON.parse(line.slice(6));

          // Server sends `event: done` as the final SSE event — exit the read loop
          if (currentEvent === 'done') {
            if (data.session_id) state.sessionId = data.session_id;
            streamDone = true;
            break;  // exit for loop
          }
          if (!firstContent && (data.type === 'text' || data.type === 'thinking' || data.type === 'tool_call')) {
            firstContent = true;
            setThinking(false);
          }

          handleSSE(data);
          if (data.type === 'tool_call') toolCount++;
          if (data.type === 'complete') totalTime = data.time || 0;
        } catch (e) {
          if (e instanceof SyntaxError) continue;
          console.warn('SSE parse error:', e);
        }
      }

      if (streamDone) break;  // exit while loop
    }

    setThinking(false);
    flushStreamBubbles();
    if (toolCount > 0 || totalTime > 0) {
      appendBubble({ role: 'sep', text: `${toolCount} tools · ${fmtTime(totalTime)}` });
    }
    updateStatus();
  } catch (e: unknown) {
    if (e instanceof DOMException && e.name === 'AbortError') {
      appendBubble({ role: 'status', text: '⏹ Stopped' });
    } else if (e instanceof Error) {
      appendBubble({ role: 'error', text: `Error: ${e.message}` });
    } else {
      appendBubble({ role: 'error', text: 'Unknown error' });
    }
  } finally {
    setStreaming(false);
    setThinking(false);
    activeReq = null;
    safeFocusInput();
    updateStatus();
  }
}

// ── SSE Event Handler ────────────────────────────────────────────────────────────

function handleSSE(evt: SSEEvent): void {
  switch (evt.type) {
    case 'text':
      appendStreamBubble(evt.text || '', 'agent');
      break;
    case 'thinking':
      appendStreamBubble(evt.text || '', 'think');
      break;
    case 'tool_call':
      appendBubble({ role: 'tool', text: `◆ ${evt.tool}  ${fmtArgs(evt.args)}` });
      break;
    case 'tool_result':
      if (evt.ok) {
        const out = (evt.output || '').replace(/\n/g, ' ').slice(0, TOOL_OUTPUT_MAX_LEN);
        appendBubble({ role: 'tool-ok', text: `✓ ${out}` });
      } else {
        appendBubble({ role: 'tool-err', text: `✗ ${(evt.error || 'unknown').slice(0, TOOL_OUTPUT_MAX_LEN)}` });
      }
      break;
    case 'error':
      appendBubble({ role: 'error', text: `● ${evt.error}` });
      break;
    case 'complete':
      state.tokens += (evt.tools || 0) * TOKEN_ESTIMATE_PER_TOOL;
      updateStatus();
      break;
    // 'done' handled inline in sendChat (session_id capture)
  }
}

function fmtArgs(args?: Record<string, unknown>): string {
  if (!args) return '';
  const parts: string[] = [];
  for (const [k, v] of Object.entries(args)) {
    if (typeof v === 'string') {
      parts.push(`${k}="${v.length > ARG_STRING_MAX_LEN ? v.slice(0, ARG_STRING_MAX_LEN) + '…' : v}"`);
    } else {
      const s = JSON.stringify(v);
      parts.push(`${k}=${s.length > ARG_STRING_MAX_LEN ? s.slice(0, ARG_STRING_MAX_LEN) + '…' : s}`);
    }
  }
  return parts.join('  ');
}

// ── Slash Commands ───────────────────────────────────────────────────────────────

function onInputChange(): void {
  const val = el.input.value;
  if (val.startsWith('/')) {
    showPopup(val);
  } else {
    hidePopup();
  }
}

function onInputKey(e: KeyboardEvent): void {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    const val = el.input.value.trim();
    if (val.startsWith('/') && popupIdx >= 0 && el.popup.classList.contains('show')) {
      selectPopupItem(popupIdx);
      return;
    }
    send();
  } else if (e.key === 'ArrowDown' && el.popup.classList.contains('show')) {
    e.preventDefault();
    popupIdx = Math.min(popupIdx + 1, el.popup.children.length - 1);
    highlightPopup();
  } else if (e.key === 'ArrowUp' && el.popup.classList.contains('show')) {
    e.preventDefault();
    popupIdx = Math.max(popupIdx - 1, 0);
    highlightPopup();
  } else if (e.key === 'Escape') {
    hidePopup();
  }
}

function showPopup(filter: string): void {
  const matches = state.commands.filter(([name]) => name.startsWith(filter));
  if (matches.length === 0 || (matches.length === 1 && matches[0][0] === filter)) {
    hidePopup();
    return;
  }

  el.popup.innerHTML = matches.map(([name, desc], i) =>
    `<div class="cmd-item${i === 0 ? ' active' : ''}" data-idx="${i}">
       <span class="name">${esc(name)}</span>
       <span class="desc">${esc(desc)}</span>
     </div>`
  ).join('');
  el.popup.classList.add('show');
  popupIdx = 0;
}

function hidePopup(): void {
  el.popup.classList.remove('show');
  popupIdx = -1;
}

function highlightPopup(): void {
  const items = el.popup.querySelectorAll('.cmd-item');
  items.forEach((item, i) => item.classList.toggle('active', i === popupIdx));
}

function selectPopupItem(idx: number): void {
  const items = el.popup.querySelectorAll('.cmd-item');
  const itemEl = items[idx] as HTMLElement | undefined;
  if (!itemEl) return;
  const name = itemEl.querySelector('.name')?.textContent || '';
  el.input.value = name + ' ';
  el.input.focus();
  hidePopup();
}

// ── Actions ──────────────────────────────────────────────────────────────────────

function send(): void {
  const val = el.input.value.trim();
  if (!val) return;
  sendChat(val);
}

function sendQuick(cmd: string): void {
  el.input.value = cmd;
  sendChat(cmd);
}

function stop(): void {
  if (activeReq) {
    activeReq.abort();
    activeReq = null;
  }
  setStreaming(false);
  setThinking(false);
  safeFocusInput();
}

function onModelChange(): void {
  state.model = el.modelSelect.value;
  updateStatus();
}

function toggleSidebar(): void {
  document.getElementById('sidebar')!.classList.toggle('open');
  document.getElementById('overlay')!.classList.toggle('show');
}

// ── Event Binding (replaces HTML onclick/onkeydown/oninput) ──────────────────────

function bindEvents(): void {
  // Input
  el.input.addEventListener('input', onInputChange);
  el.input.addEventListener('keydown', onInputKey);

  // Buttons
  el.sendBtn.addEventListener('click', send);
  el.stopBtn.addEventListener('click', stop);

  // Model selector
  el.modelSelect.addEventListener('change', onModelChange);

  // Hamburger + overlay
  document.querySelector('.hamburger')?.addEventListener('click', toggleSidebar);
  document.getElementById('overlay')?.addEventListener('click', toggleSidebar);

  // Sidebar shortcut buttons
  document.querySelectorAll('.shortcut[data-cmd]').forEach(btn => {
    const cmd = (btn as HTMLElement).dataset.cmd;
    if (cmd) btn.addEventListener('click', () => sendQuick(cmd));
  });

  // Popup item clicks via event delegation (replaces onmousedown inline)
  el.popup.addEventListener('mousedown', (e) => {
    const item = (e.target as HTMLElement).closest('.cmd-item') as HTMLElement | undefined;
    if (item && item.dataset.idx !== undefined) {
      const idx = parseInt(item.dataset.idx, 10);
      if (!isNaN(idx)) selectPopupItem(idx);
    }
  });
}

// ── Init ─────────────────────────────────────────────────────────────────────────

async function main(): Promise<void> {
  bindEvents();
  await fetchInit();

  try {
    const skillsData = await apiGet('/skills') as {
      skills: Array<{ name: string; description: string; triggers: string[] }>;
    };
    state.commands = (skillsData.skills || []).map(s => ['/' + s.name, s.description] as [string, string]);

    const coreCmds: Array<[string, string]> = [
      ['/help', 'Show help'],
      ['/clear', 'Clear conversation'],
      ['/compact', 'Compact history'],
      ['/context', 'Show token usage'],
      ['/cost', 'Estimate cost'],
      ['/model', 'Change model'],
      ['/workspace', 'Change workspace'],
      ['/skills', 'List skills'],
      ['/skill', 'Switch skill'],
      ['/history', 'Browse sessions'],
      ['/save', 'Save session'],
      ['/undo', 'Undo changes'],
      ['/review', 'Code review'],
      ['/fix', 'Auto-fix issues'],
      ['/git', 'Git operations'],
      ['/dangerous', 'Dangerous mode'],
      ['/think', 'Thinking mode'],
    ];
    for (const cmd of coreCmds) {
      if (!state.commands.find(c => c[0] === cmd[0])) {
        state.commands.push(cmd);
      }
    }
    state.commands.sort((a, b) => a[0].localeCompare(b[0]));
  } catch {
    // Commands not essential — UI works without them
  }

  el.input.focus();
  updateStatus();
}

main();
