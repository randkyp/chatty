const ws = new WebSocket(`ws://${window.location.host}/ws`);
const history = document.getElementById('chat-history');
const input = document.getElementById('user-input');

let currentAssistantDiv = null;
let currentAssistantContent = "";
let thinkingInterval = null;
let thinkingStartTime = null;
let currentCommandDiv = null;

// Configure marked.js to support breaks
marked.setOptions({
    breaks: true,
});

// Configure KaTeX extension
marked.use(window.markedKatex({ throwOnError: false }));

// Configure DOMPurify to allow standard markdown HTML and MathML for KaTeX
const purifyConfig = {
    USE_PROFILES: { html: true, mathMl: true },
    ADD_ATTR: ['style', 'target', 'class'] // Ensure style and class are preserved for KaTeX formatting
};

// Handle auto-resizing
input.addEventListener('input', function() {
    this.style.height = 'auto';
    this.style.height = (this.scrollHeight) + 'px';
});

// Submit on Enter (Shift+Enter for newline)
input.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
});

function sendMessage() {
    const text = input.value.trim();
    if (!text) return;
    
    // Create user message element
    const userDiv = document.createElement('div');
    userDiv.className = 'message user';
    userDiv.textContent = `> ${text}`;
    history.appendChild(userDiv);
    
    // Scroll to the bottom of the page
    window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });

    // Local command handling for themes
    const parts = text.split(/\s+/);
    if (parts[0] === '/theme') {
        const theme = parts[1];
        if (theme === 'dark' || theme === 'light') {
            document.documentElement.setAttribute('data-theme', theme);
            appendSystemMessage(`Theme set to ${theme}`);
        } else {
            document.documentElement.removeAttribute('data-theme');
            appendSystemMessage(`Theme set to auto (OS preference)`);
        }
    } else {
        // Send to WebSocket backend
        ws.send(JSON.stringify({ type: 'message', text: text }));
    }
    
    input.value = '';
    input.style.height = 'auto';
}

function appendSystemMessage(text, type='system') {
    const div = document.createElement('div');
    div.className = `message ${type}`;
    div.textContent = text;
    history.appendChild(div);
}

ws.onmessage = function(event) {
    const data = JSON.parse(event.data);
    
    if (data.type === 'welcome') {
        appendSystemMessage(data.content);
    } else if (data.type === 'system') {
        appendSystemMessage(data.content);
    } else if (data.type === 'warning') {
        appendSystemMessage(data.content, 'warning');
    } else if (data.type === 'error') {
        if (thinkingInterval) {
            clearInterval(thinkingInterval);
            thinkingInterval = null;
            if (currentAssistantDiv && currentAssistantContent === "") {
                currentAssistantDiv.innerHTML = "";
            }
        }
        appendSystemMessage(data.content, 'error');
    } else if (data.type === 'command_start') {
        currentCommandDiv = document.createElement('div');
        currentCommandDiv.className = 'message system';
        currentCommandDiv.innerHTML = `<em>Running ${data.content}...</em>`;
        history.appendChild(currentCommandDiv);
    } else if (data.type === 'command_end') {
        if (currentCommandDiv) {
            currentCommandDiv.remove();
            currentCommandDiv = null;
        }
    } else if (data.type === 'theme') {
        // Ignored, handled locally
    } else if (data.type === 'stream_start') {
        currentAssistantDiv = document.createElement('div');
        currentAssistantDiv.className = 'message assistant';
        history.appendChild(currentAssistantDiv);
        currentAssistantContent = "";
        
        currentAssistantDiv.innerHTML = '<em>Thinking... 0.0s</em>';
        thinkingStartTime = Date.now();
        thinkingInterval = setInterval(() => {
            const elapsed = ((Date.now() - thinkingStartTime) / 1000).toFixed(1);
            if (currentAssistantDiv && currentAssistantContent === "") {
                currentAssistantDiv.innerHTML = `<em>Thinking... ${elapsed}s</em>`;
            }
        }, 100);
    } else if (data.type === 'stream_chunk') {
        if (thinkingInterval) {
            clearInterval(thinkingInterval);
            thinkingInterval = null;
        }
        currentAssistantContent += data.content;
        if (currentAssistantDiv) {
            // Re-parse markdown for every chunk (can be optimized, but fine for local UI)
            const html = marked.parse(currentAssistantContent);
            currentAssistantDiv.innerHTML = DOMPurify.sanitize(html, purifyConfig);
        }
    } else if (data.type === 'stream_end') {
        if (thinkingInterval) {
            clearInterval(thinkingInterval);
            thinkingInterval = null;
            if (currentAssistantDiv && currentAssistantContent === "") {
                currentAssistantDiv.innerHTML = "";
            }
        }
        currentAssistantDiv = null;
        currentAssistantContent = "";
    }
};

ws.onclose = function() {
    appendSystemMessage("Connection to server closed.", 'error');
};

// --- Column Resizing Logic ---
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

document.querySelectorAll('.resize-handle').forEach(handle => {
    handle.addEventListener('mousedown', function(e) {
        isResizing = true;
        document.body.style.cursor = 'col-resize';
        document.addEventListener('mousemove', onMouseMove);
        document.addEventListener('mouseup', onMouseUp);
        e.preventDefault();
    });
});
