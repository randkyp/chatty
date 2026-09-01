// Chatty web UI front-end.
//
// Talks to the FastAPI WebSocket at /ws. Features: auto-reconnect, a Stop button
// that cancels in-flight generation, throttled markdown rendering, image
// paste/drop, persisted theme + chat history (display-only) + input history.

const history = document.getElementById('chat-history');
const input = document.getElementById('user-input');
const stopButton = document.getElementById('stop-button');

let ws = null;
let reconnectDelay = 500;            // ms, backs off to a cap
let reconnectAttempts = 0;
let manualClose = false;

let currentAssistantDiv = null;
let currentAssistantContent = '';
let thinkingInterval = null;
let thinkingStartTime = null;
let currentCommandDiv = null;
let renderScheduled = false;         // throttle flag for markdown re-render
let streaming = false;

const pendingImages = [];            // [{data_url, mime_type}]

const THEME_KEY = 'chatty-theme';
const INPUT_KEY = 'chatty-input-history';

// ── marked / KaTeX / DOMPurify setup ───────────────────────────────────────
marked.setOptions({ breaks: true });
marked.use(window.markedKatex({ throwOnError: false, nonStandard: true }));
const purifyConfig = {
    USE_PROFILES: { html: true, mathMl: true },
    ADD_ATTR: ['style', 'target', 'class'],
};

function renderMarkdown(text) {
    return DOMPurify.sanitize(marked.parse(text), purifyConfig);
}

// ── Persistence ─────────────────────────────────────────────────────────────
function loadStore(key, fallback) {
    try {
        return JSON.parse(localStorage.getItem(key)) ?? fallback;
    } catch {
        return fallback;
    }
}
function saveStore(key, value) {
    try {
        localStorage.setItem(key, JSON.stringify(value));
    } catch { /* quota / private mode */ }
}

let inputHistory = loadStore(INPUT_KEY, []);
let inputHistoryIndex = inputHistory.length;

// ── Theme (persisted) ───────────────────────────────────────────────────────
function applyTheme(theme) {
    if (theme === 'dark' || theme === 'light') {
        document.documentElement.setAttribute('data-theme', theme);
    } else {
        document.documentElement.removeAttribute('data-theme');
    }
    saveStore(THEME_KEY, theme || 'auto');
}
applyTheme(loadStore(THEME_KEY, 'auto'));

// ── Rendering helpers ───────────────────────────────────────────────────────
function scrollToBottom() {
    window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
}

function appendSystemMessage(text, type = 'system') {
    const div = document.createElement('div');
    div.className = `message ${type}`;
    div.textContent = text;
    history.appendChild(div);
    return div;
}

function appendUserMessage(text) {
    const div = document.createElement('div');
    div.className = 'message user';
    div.textContent = `> ${text}`;
    history.appendChild(div);
}

function appendAssistantMessage(markdownText) {
    const div = document.createElement('div');
    div.className = 'message assistant';
    div.innerHTML = renderMarkdown(markdownText);
    history.appendChild(div);
}



function setStreaming(on) {
    streaming = on;
    stopButton.hidden = !on;
}

function stopThinking() {
    if (thinkingInterval) {
        clearInterval(thinkingInterval);
        thinkingInterval = null;
    }
}

// ── WebSocket with auto-reconnect ───────────────────────────────────────────
function connect() {
    ws = new WebSocket(`ws://${window.location.host}/ws`);

    ws.onopen = () => {
        reconnectDelay = 500;
        reconnectAttempts = 0;
    };

    ws.onmessage = (event) => {
        let data;
        try { data = JSON.parse(event.data); } catch { return; }
        handleServerMessage(data);
    };

    ws.onclose = () => {
        setStreaming(false);
        stopThinking();
        if (manualClose) return;

        if (reconnectAttempts >= 5) {
            appendSystemMessage('Connection lost — maximum reconnect attempts reached. Please refresh the page.', 'error');
            return;
        }

        reconnectAttempts++;
        appendSystemMessage(`Connection lost — reconnecting in ${(reconnectDelay / 1000).toFixed(1)}s (attempt ${reconnectAttempts}/5)…`, 'error');
        setTimeout(connect, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 2, 10000);
    };

    ws.onerror = () => { /* onclose handles reconnect */ };
}

function handleServerMessage(data) {
    switch (data.type) {
        case 'welcome':
        case 'system':
            appendSystemMessage(data.content);
            break;
        case 'warning':
            appendSystemMessage(data.content, 'warning');
            break;
        case 'error':
            stopThinking();
            if (currentAssistantDiv && currentAssistantContent === '') {
                currentAssistantDiv.innerHTML = '';
            }
            appendSystemMessage(data.content, 'error');
            break;
        case 'command_start':
            currentCommandDiv = document.createElement('div');
            currentCommandDiv.className = 'message system';
            currentCommandDiv.innerHTML = `<em>Running ${data.content}...</em>`;
            history.appendChild(currentCommandDiv);
            break;
        case 'command_end':
            if (currentCommandDiv) { currentCommandDiv.remove(); currentCommandDiv = null; }
            break;
        case 'theme':
            break; // handled client-side on send
        case 'copy_to_clipboard':
            navigator.clipboard.writeText(data.content)
                .then(() => appendSystemMessage('Last response copied to clipboard.'))
                .catch((err) => appendSystemMessage(`Failed to copy: ${err}`, 'error'));
            break;
        case 'clear_dom':
            history.innerHTML = '';
            break;
        case 'remove_last_exchange': {
            const messages = history.querySelectorAll('.message.assistant, .message.user');
            if (messages.length >= 2) {
                messages[messages.length - 1].remove();
                messages[messages.length - 2].remove();
            } else if (messages.length === 1) {
                messages[0].remove();
            }
            break;
        }
        case 'remove_last_assistant': {
            const assistantMsgs = history.querySelectorAll('.message.assistant');
            if (assistantMsgs.length > 0) {
                assistantMsgs[assistantMsgs.length - 1].remove();
            }
            break;
        }
        case 'load_history': {
            history.innerHTML = '';
            const messages = data.content;
            messages.forEach(msg => {
                if (msg.role === 'user') {
                    let text = '';
                    if (typeof msg.content === 'string') text = msg.content;
                    else if (Array.isArray(msg.content)) {
                        text = msg.content.filter(p => p.type === 'text').map(p => p.text).join('');
                    }
                    appendUserMessage(text);
                } else if (msg.role === 'assistant') {
                    let text = '';
                    if (typeof msg.content === 'string') text = msg.content;
                    else if (Array.isArray(msg.content)) {
                        text = msg.content.filter(p => p.type === 'text').map(p => p.text).join('');
                    }
                    appendAssistantMessage(text);
                }
            });
            scrollToBottom();
            break;
        }
        case 'stream_start':
            setStreaming(true);
            currentAssistantDiv = document.createElement('div');
            currentAssistantDiv.className = 'message assistant';
            history.appendChild(currentAssistantDiv);
            currentAssistantContent = '';
            currentAssistantDiv.innerHTML = '<em>Thinking... 0.0s</em>';
            thinkingStartTime = Date.now();
            thinkingInterval = setInterval(() => {
                const elapsed = ((Date.now() - thinkingStartTime) / 1000).toFixed(1);
                if (currentAssistantDiv && currentAssistantContent === '') {
                    currentAssistantDiv.innerHTML = `<em>Thinking... ${elapsed}s</em>`;
                }
            }, 100);
            break;
        case 'stream_chunk':
            stopThinking();
            currentAssistantContent += data.content;
            scheduleRender();
            break;
        case 'stream_end':
            stopThinking();
            setStreaming(false);
            if (currentAssistantDiv) {
                if (currentAssistantContent === '') {
                    currentAssistantDiv.remove();
                } else {
                    currentAssistantDiv.innerHTML = renderMarkdown(currentAssistantContent);
                }
            }
            currentAssistantDiv = null;
            currentAssistantContent = '';
            break;
    }
}

// Throttle markdown re-parsing to once per animation frame instead of per chunk
// (the old code re-parsed the whole response on every chunk — O(n²)).
function scheduleRender() {
    if (renderScheduled) return;
    renderScheduled = true;
    requestAnimationFrame(() => {
        renderScheduled = false;
        if (currentAssistantDiv) {
            currentAssistantDiv.innerHTML = renderMarkdown(currentAssistantContent);
        }
    });
}

// ── Sending ─────────────────────────────────────────────────────────────────
function sendMessage() {
    const text = input.value.trim();
    if (!text && pendingImages.length === 0) return;

    if (text) {
        appendUserMessage(text);
        inputHistory.push(text);
        if (inputHistory.length > 200) inputHistory = inputHistory.slice(-200);
        saveStore(INPUT_KEY, inputHistory);
        inputHistoryIndex = inputHistory.length;
    }
    scrollToBottom();

    // Theme switching is handled locally without a round-trip.
    const parts = text.split(/\s+/);
    if (parts[0] === '/theme') {
        const theme = parts[1];
        applyTheme(theme);
        appendSystemMessage(theme === 'dark' || theme === 'light'
            ? `Theme set to ${theme}` : 'Theme set to auto (OS preference)');
    } else if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'message', text, images: pendingImages.slice() }));
    } else {
        appendSystemMessage('Not connected to server.', 'error');
    }

    pendingImages.length = 0;
    renderImageChips();
    input.value = '';
    input.style.height = 'auto';
}

function cancelGeneration() {
    if (streaming && ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'cancel' }));
    }
}

// ── Image attachments (paste + drag/drop) ────────────────────────────────────
function addImageFile(file) {
    if (!file || !file.type.startsWith('image/')) return;
    const reader = new FileReader();
    reader.onload = () => {
        pendingImages.push({ data_url: reader.result, mime_type: file.type });
        renderImageChips();
    };
    reader.readAsDataURL(file);
}

function renderImageChips() {
    let bar = document.getElementById('image-chips');
    if (!bar) {
        bar = document.createElement('div');
        bar.id = 'image-chips';
        const wrapper = document.getElementById('input-wrapper');
        wrapper.parentElement.insertBefore(bar, wrapper);
    }
    bar.innerHTML = '';
    pendingImages.forEach((img, i) => {
        const chip = document.createElement('span');
        chip.className = 'image-chip';
        chip.textContent = `🖼 image ${i + 1} ✕`;
        chip.title = 'Click to remove';
        chip.onclick = () => { pendingImages.splice(i, 1); renderImageChips(); };
        bar.appendChild(chip);
    });
    bar.style.display = pendingImages.length ? 'flex' : 'none';
}

// ── Input UX: autoresize, submit, history, paste, drag/drop ──────────────────
input.addEventListener('input', function () {
    this.style.height = 'auto';
    this.style.height = `${this.scrollHeight}px`;
});

input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
        return;
    }
    if (e.key === 'Escape') {
        cancelGeneration();
        return;
    }
    // Up/Down cycle through previously sent inputs.
    if (e.key === 'ArrowUp' && (input.value === '' || inputHistoryIndex < inputHistory.length)) {
        if (inputHistoryIndex > 0) {
            e.preventDefault();
            inputHistoryIndex--;
            input.value = inputHistory[inputHistoryIndex] || '';
        }
    } else if (e.key === 'ArrowDown' && inputHistoryIndex < inputHistory.length) {
        e.preventDefault();
        inputHistoryIndex++;
        input.value = inputHistory[inputHistoryIndex] || '';
    }
});

input.addEventListener('paste', (e) => {
    const items = e.clipboardData?.items || [];
    for (const item of items) {
        if (item.kind === 'file' && item.type.startsWith('image/')) {
            addImageFile(item.getAsFile());
            e.preventDefault();
        }
    }
});

['dragover', 'drop'].forEach((evt) => {
    document.addEventListener(evt, (e) => {
        e.preventDefault();
        if (evt === 'drop') {
            for (const f of e.dataTransfer.files) addImageFile(f);
        }
    });
});

stopButton.addEventListener('click', cancelGeneration);

window.addEventListener('beforeunload', () => { manualClose = true; });

connect();

// ── Column resizing (unchanged) ─────────────────────────────────────────────
const paperContainer = document.getElementById('paper-container');
let isResizing = false;

function onMouseMove(e) {
    if (!isResizing) return;
    const centerX = window.innerWidth / 2;
    const distanceX = Math.abs(e.clientX - centerX);
    const newWidth = Math.max(300, distanceX * 2);
    paperContainer.style.maxWidth = `${newWidth}px`;
}

function onMouseUp() {
    if (isResizing) {
        isResizing = false;
        document.body.style.cursor = '';
        document.removeEventListener('mousemove', onMouseMove);
        document.removeEventListener('mouseup', onMouseUp);
    }
}

document.querySelectorAll('.resize-handle').forEach((handle) => {
    handle.addEventListener('mousedown', (e) => {
        isResizing = true;
        document.body.style.cursor = 'col-resize';
        document.addEventListener('mousemove', onMouseMove);
        document.addEventListener('mouseup', onMouseUp);
        e.preventDefault();
    });
});
