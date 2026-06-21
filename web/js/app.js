"use strict";
const TOKEN_ESTIMATE_PER_TOOL = 200;
const ARG_STRING_MAX_LEN = 60;
const TOOL_OUTPUT_MAX_LEN = 150;
const state = {
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
    chat: document.getElementById('chat'),
    input: document.getElementById('cmd-input'),
    sendBtn: document.getElementById('send-btn'),
    stopBtn: document.getElementById('stop-btn'),
    popup: document.getElementById('cmd-popup'),
    dot: document.getElementById('status-dot'),
    subtitle: document.getElementById('top-subtitle'),
    initInfo: document.getElementById('init-info'),
    modelSelect: document.getElementById('model-select'),
    skillTags: document.getElementById('skill-tags'),
    wsPath: document.getElementById('ws-path'),
    stTokens: document.getElementById('st-tokens'),
    stSkill: document.getElementById('st-skill'),
    stModel: document.getElementById('st-model'),
    stTime: document.getElementById('st-time'),
};
let popupIdx = -1;
let activeReq = null;
function esc(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
}
function fmtTime(s) { return s.toFixed(1) + 's'; }
function appendBubble(msg) {
    const div = document.createElement('div');
    div.className = `msg ${msg.role}`;
    div.innerHTML = renderMarkdown(msg.text);
    el.chat.appendChild(div);
    scrollBottom();
}
function appendStreamBubble(text, cls) {
    let div = el.chat.lastElementChild;
    if (!div || !div.classList.contains(cls) || !div.classList.contains('streaming')) {
        div = document.createElement('div');
        div.className = `msg ${cls} streaming`;
        div.dataset.raw = text;
        div.innerHTML = renderMarkdown(text);
        el.chat.appendChild(div);
    }
    else {
        div.dataset.raw = (div.dataset.raw || '') + text;
        div.innerHTML = renderMarkdown(div.dataset.raw);
    }
    scrollBottom();
    return div;
}
function flushStreamBubbles() {
    el.chat.querySelectorAll('.streaming').forEach(d => {
        const el = d;
        el.classList.remove('streaming');
        delete el.dataset.raw;
    });
}
function renderMarkdown(text) {
    const codeBlocks = [];
    let html = esc(text);
    html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
        const idx = codeBlocks.length;
        codeBlocks.push(`<pre><code class="${esc(lang)}">${esc(code.trimEnd())}</code></pre>`);
        return `\x00CB${idx}\x00`;
    });
    html = html.replace(/(^|\n)### (.+?)(?=\n|$)/gm, '$1</p><h3>$2</h3><p>');
    html = html.replace(/(^|\n)## (.+?)(?=\n|$)/gm, '$1</p><h2>$2</h2><p>');
    html = html.replace(/(^|\n)# (.+?)(?=\n|$)/gm, '$1</p><h1>$2</h1><p>');
    html = html.replace(/\n\n/g, '</p><p>');
    html = html.replace(/\n/g, '<br>');
    html = `<p>${html}</p>`;
    html = html.replace(/<p><\/p>/g, '');
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
    html = html.replace(/\x00CB(\d+)\x00/g, (_m, idx) => codeBlocks[parseInt(idx)]);
    return html;
}
function scrollBottom() {
    requestAnimationFrame(() => {
        el.chat.scrollTop = el.chat.scrollHeight;
    });
}
function setThinking(on) {
    state.thinking = on;
    el.dot.className = `dot ${on ? 'thinking' : 'ready'}`;
    el.subtitle.textContent = on ? 'Thinking…' : '';
}
function updateStatus() {
    el.stTokens.textContent = `tokens: ~${state.tokens.toLocaleString()}`;
    el.stSkill.textContent = `skill: ${state.skill}`;
    el.stModel.textContent = `model: ${state.model}`;
    if (state.startTime > 0) {
        el.stTime.textContent = fmtTime((Date.now() - state.startTime) / 1000);
    }
}
function setStreaming(on) {
    state.streaming = on;
    document.body.classList.toggle('streaming', on);
    el.input.disabled = on;
}
function safeFocusInput() {
    requestAnimationFrame(() => {
        el.input.disabled = false;
        el.input.focus();
    });
}
async function apiGet(path) {
    const r = await fetch(path);
    if (!r.ok)
        throw new Error(`HTTP ${r.status}`);
    return r.json();
}
async function apiPost(path, body) {
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
async function fetchInit() {
    try {
        const [health, skills, models] = await Promise.all([
            apiGet('/health'),
            apiGet('/skills'),
            apiGet('/models'),
        ]);
        state.model = health.model;
        state.workspace = health.workspace;
        el.modelSelect.innerHTML = (models.models || []).map(m => `<option ${m.id === models.current ? 'selected' : ''}>${m.id}</option>`).join('');
        if ((models.models || []).length === 0) {
            el.modelSelect.innerHTML = `<option>${state.model}</option>`;
        }
        el.skillTags.innerHTML = (skills.skills || []).map(s => `<span class="skill-tag">${esc(s.name)}</span>`).join('');
        el.wsPath.textContent = state.workspace;
        el.initInfo.textContent = `Model: ${state.model}  ·  Workspace: ${state.workspace}`;
        el.stModel.textContent = `model: ${state.model}`;
        updateStatus();
    }
    catch {
        el.initInfo.textContent = '⚠ Server unreachable — start with: ata gui';
    }
}
async function sendChat(message) {
    if (state.streaming || !message.trim())
        return;
    appendBubble({ role: 'user', text: message });
    el.input.value = '';
    hidePopup();
    flushStreamBubbles();
    state.startTime = Date.now();
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
        if (!reader)
            throw new Error('No response body');
        const decoder = new TextDecoder();
        let buffer = '';
        let currentEvent = '';
        let toolCount = 0;
        let totalTime = 0;
        let firstContent = false;
        let streamDone = false;
        while (true) {
            const { done, value } = await reader.read();
            if (done)
                break;
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop() || '';
            for (const line of lines) {
                if (line.startsWith('event: ')) {
                    currentEvent = line.slice(7).trim();
                    continue;
                }
                if (!line.startsWith('data: '))
                    continue;
                try {
                    const data = JSON.parse(line.slice(6));
                    if (currentEvent === 'done') {
                        if (data.session_id)
                            state.sessionId = data.session_id;
                        streamDone = true;
                        break;
                    }
                    if (!firstContent && (data.type === 'text' || data.type === 'thinking' || data.type === 'tool_call')) {
                        firstContent = true;
                        setThinking(false);
                    }
                    handleSSE(data);
                    if (data.type === 'tool_call')
                        toolCount++;
                    if (data.type === 'complete')
                        totalTime = data.time || 0;
                }
                catch (e) {
                    if (e instanceof SyntaxError)
                        continue;
                    console.warn('SSE parse error:', e);
                }
            }
            if (streamDone)
                break;
        }
        setThinking(false);
        flushStreamBubbles();
        if (toolCount > 0 || totalTime > 0) {
            appendBubble({ role: 'sep', text: `${toolCount} tools · ${fmtTime(totalTime)}` });
        }
        updateStatus();
    }
    catch (e) {
        if (e instanceof DOMException && e.name === 'AbortError') {
            appendBubble({ role: 'status', text: '⏹ Stopped' });
        }
        else if (e instanceof Error) {
            appendBubble({ role: 'error', text: `Error: ${e.message}` });
        }
        else {
            appendBubble({ role: 'error', text: 'Unknown error' });
        }
    }
    finally {
        setStreaming(false);
        setThinking(false);
        activeReq = null;
        safeFocusInput();
        updateStatus();
    }
}
function handleSSE(evt) {
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
            }
            else {
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
    }
}
function fmtArgs(args) {
    if (!args)
        return '';
    const parts = [];
    for (const [k, v] of Object.entries(args)) {
        if (typeof v === 'string') {
            parts.push(`${k}="${v.length > ARG_STRING_MAX_LEN ? v.slice(0, ARG_STRING_MAX_LEN) + '…' : v}"`);
        }
        else {
            const s = JSON.stringify(v);
            parts.push(`${k}=${s.length > ARG_STRING_MAX_LEN ? s.slice(0, ARG_STRING_MAX_LEN) + '…' : s}`);
        }
    }
    return parts.join('  ');
}
function onInputChange() {
    const val = el.input.value;
    if (val.startsWith('/')) {
        showPopup(val);
    }
    else {
        hidePopup();
    }
}
function onInputKey(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        const val = el.input.value.trim();
        if (val.startsWith('/') && popupIdx >= 0 && el.popup.classList.contains('show')) {
            selectPopupItem(popupIdx);
            return;
        }
        send();
    }
    else if (e.key === 'ArrowDown' && el.popup.classList.contains('show')) {
        e.preventDefault();
        popupIdx = Math.min(popupIdx + 1, el.popup.children.length - 1);
        highlightPopup();
    }
    else if (e.key === 'ArrowUp' && el.popup.classList.contains('show')) {
        e.preventDefault();
        popupIdx = Math.max(popupIdx - 1, 0);
        highlightPopup();
    }
    else if (e.key === 'Escape') {
        hidePopup();
    }
}
function showPopup(filter) {
    const matches = state.commands.filter(([name]) => name.startsWith(filter));
    if (matches.length === 0 || (matches.length === 1 && matches[0][0] === filter)) {
        hidePopup();
        return;
    }
    el.popup.innerHTML = matches.map(([name, desc], i) => `<div class="cmd-item${i === 0 ? ' active' : ''}" data-idx="${i}">
       <span class="name">${esc(name)}</span>
       <span class="desc">${esc(desc)}</span>
     </div>`).join('');
    el.popup.classList.add('show');
    popupIdx = 0;
}
function hidePopup() {
    el.popup.classList.remove('show');
    popupIdx = -1;
}
function highlightPopup() {
    const items = el.popup.querySelectorAll('.cmd-item');
    items.forEach((item, i) => item.classList.toggle('active', i === popupIdx));
}
function selectPopupItem(idx) {
    const items = el.popup.querySelectorAll('.cmd-item');
    const itemEl = items[idx];
    if (!itemEl)
        return;
    const name = itemEl.querySelector('.name')?.textContent || '';
    el.input.value = name + ' ';
    el.input.focus();
    hidePopup();
}
function send() {
    const val = el.input.value.trim();
    if (!val)
        return;
    sendChat(val);
}
function sendQuick(cmd) {
    el.input.value = cmd;
    sendChat(cmd);
}
function stop() {
    if (activeReq) {
        activeReq.abort();
        activeReq = null;
    }
    setStreaming(false);
    setThinking(false);
    safeFocusInput();
}
function onModelChange() {
    state.model = el.modelSelect.value;
    updateStatus();
}
function toggleSidebar() {
    document.getElementById('sidebar').classList.toggle('open');
    document.getElementById('overlay').classList.toggle('show');
}
function bindEvents() {
    el.input.addEventListener('input', onInputChange);
    el.input.addEventListener('paste', () => { setTimeout(onInputChange, 0); });
    el.input.addEventListener('keydown', onInputKey);
    el.sendBtn.addEventListener('click', send);
    el.stopBtn.addEventListener('click', stop);
    el.modelSelect.addEventListener('change', onModelChange);
    // Ensure model dropdown opens on click (for automation tools)
    el.modelSelect.addEventListener('mousedown', (e) => {
        el.modelSelect.focus();
        if (el.modelSelect.showPicker) el.modelSelect.showPicker();
    });
    document.querySelector('.hamburger')?.addEventListener('click', toggleSidebar);
    document.getElementById('overlay')?.addEventListener('click', toggleSidebar);
    document.querySelectorAll('.shortcut[data-cmd]').forEach(btn => {
        const cmd = btn.dataset.cmd;
        if (cmd)
            btn.addEventListener('click', () => sendQuick(cmd));
    });
    // Skill tag click: send a message to activate that skill
    el.skillTags?.addEventListener('click', (e) => {
        const tag = e.target.closest('.skill-tag');
        if (tag && tag.textContent) {
            const skillName = tag.textContent.trim();
            el.input.value = '/skill ' + skillName;
            sendChat('/skill ' + skillName);
        }
    });
    el.popup.addEventListener('mousedown', (e) => {
        const item = e.target.closest('.cmd-item');
        if (item && item.dataset.idx !== undefined) {
            const idx = parseInt(item.dataset.idx, 10);
            if (!isNaN(idx))
                selectPopupItem(idx);
        }
    });
    // Global keyboard shortcuts
    document.addEventListener('keydown', (e) => {
        if (e.ctrlKey && e.key === 'k') {
            e.preventDefault();
            el.input.focus();
            el.input.select();
        }
        if (e.key === 'Escape') {
            hidePopup();
            if (document.activeElement === el.input && el.input.value === '') {
                el.input.blur();
            }
        }
    });
}
async function main() {
    bindEvents();
    await fetchInit();
    try {
        const skillsData = await apiGet('/skills');
        state.commands = (skillsData.skills || []).map(s => ['/' + s.name, s.description]);
        const coreCmds = [
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
    }
    catch {
    }
    el.input.focus();
    updateStatus();
}
main();
