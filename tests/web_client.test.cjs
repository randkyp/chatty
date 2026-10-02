// Run with: node --test tests/web_client.test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

function client() {
    class Element {
        constructor() { this.children = []; this.style = {}; this.value = ''; this.className = ''; }
        set innerHTML(value) { this.html = value; this.children = []; }
        get innerHTML() { return this.html; }
        appendChild(child) { child.parentElement = this; this.children.push(child); }
        remove() { this.parentElement.children = this.parentElement.children.filter(c => c !== this); }
        setAttribute() {}
        removeAttribute() {}
        addEventListener() {}
        focus() {}
    }
    const elements = new Map();
    const document = {
        getElementById(id) {
            if (!elements.has(id)) elements.set(id, new Element());
            return elements.get(id);
        },
        createElement: () => new Element(),
        documentElement: new Element(), body: new Element(),
        addEventListener() {}, querySelectorAll: () => [],
    };
    class WebSocket {
        static OPEN = 1;
        constructor() { this.readyState = 1; this.sent = []; }
        send(data) { this.sent.push(JSON.parse(data)); }
    }
    const context = vm.createContext({
        document, WebSocket,
        window: { markedKatex() {}, scrollTo() {}, addEventListener() {}, location: {} },
        marked: { setOptions() {}, use() {}, parse: s => s },
        DOMPurify: { sanitize: s => s },
        localStorage: { getItem() {}, setItem() {} },
        sessionStorage: { getItem() {}, setItem() {} },
        URLSearchParams,
        setInterval: () => 1, clearInterval() {}, setTimeout() {},
        requestAnimationFrame: f => f(),
    });
    vm.runInContext(fs.readFileSync('src/chatty/web/public/app.js', 'utf8'), context);
    return code => vm.runInContext(code, context);
}

const exchange = JSON.stringify([{ role: 'user', content: 'question' }, { role: 'assistant', content: 'answer' }]);

test('welcome restores the server conversation', () => {
    const run = client();
    run(`handleServerMessage({type:'welcome', content:'Welcome', restored:true, session_id:'restored-id', history:${exchange}})`);
    assert.equal(run("history.children.filter(c => c.className === 'message user' || c.className === 'message assistant').length"), 2);
    assert.equal(run('history.children[0].textContent'), '> question');
    assert.equal(run("history.children.some(c => c.textContent === 'Welcome Conversation restored.')"), true);
});

test('/undo replaces conversation, not the command display', () => {
    const run = client();
    run(`handleServerMessage({type:'history', content:${exchange}}); input.value='/undo'; sendMessage();`);
    assert.equal(run("history.children.filter(c => c.className === 'message user' || c.className === 'message assistant').length"), 2);
    run("handleServerMessage({type:'submission_done', content:[], accepted:true})");
    assert.equal(run("history.children.filter(c => c.className === 'message user' || c.className === 'message assistant').length"), 0);
    assert.equal(run('input.value'), '');
    assert.equal(run("history.children.some(c => c.textContent === '> /undo')"), true);
});

test('/btw stays outside authoritative history when retry replaces the answer', () => {
    const run = client();
    run(`handleServerMessage({type:'history', content:${exchange}});
        input.value='/btw aside'; sendMessage();
        handleServerMessage({type:'stream_start', ephemeral:true});
        handleServerMessage({type:'stream_chunk', content:'aside answer'});
        handleServerMessage({type:'stream_end'});
        handleServerMessage({type:'submission_done', content:${exchange}, accepted:true});
        input.value='/retry'; sendMessage();
        handleServerMessage({type:'history', content:[{role:'user', content:'question'}]});
        handleServerMessage({type:'stream_start', ephemeral:false});
        handleServerMessage({type:'stream_chunk', content:'replacement'});
        handleServerMessage({type:'stream_end'});
        handleServerMessage({type:'submission_done', accepted:true, content:[
            {role:'user', content:'question'}, {role:'assistant', content:'replacement'}]});`);
    assert.equal(run("history.children.filter(c => c.className === 'message user' || c.className === 'message assistant').length"), 2);
    assert.equal(run("history.children.find(c => c.className === 'message assistant').innerHTML"), 'replacement');
    assert.equal(run("history.children.find(c => c.className === 'message assistant ephemeral').innerHTML"), 'aside answer');
    assert.equal(run("history.children.some(c => c.className === 'message assistant pending')"), false);
});

test('busy, rejected, failed and disconnected sends retain text and attachments', () => {
    const run = client();
    run("input.value='draft'; pendingImages.push({data_url:'data:image/png;base64,AA'}); setStreaming(true); sendMessage();");
    assert.equal(run('ws.sent.length'), 0);
    assert.equal(run('input.value'), 'draft');
    assert.equal(run('pendingImages.length'), 1);
    run("setStreaming(false); sendMessage(); handleServerMessage({type:'submission_rejected', content:'busy'});");
    assert.equal(run('input.value'), 'draft');
    assert.equal(run('pendingImages.length'), 1);
    run("sendMessage(); handleServerMessage({type:'submission_done', content:[], accepted:false});");
    assert.equal(run('input.value'), 'draft');
    assert.equal(run('pendingImages.length'), 1);
    assert.equal(run("history.children.filter(c => c.className === 'message user' || c.className === 'message assistant').length"), 0);
    run('ws.readyState=3; sendMessage();');
    assert.equal(run('input.value'), 'draft');
    assert.equal(run('pendingImages.length'), 1);
});

test('new stream follows earlier system messages in visible order', () => {
    const run = client();
    run(`handleServerMessage({type:'welcome', content:'Welcome', history:[]});
        handleServerMessage({type:'system', content:'Earlier status'});
        handleServerMessage({type:'history', content:[{role:'user', content:'Hello'}]});
        handleServerMessage({type:'stream_start', ephemeral:false});`);
    assert.deepEqual(Array.from(run('history.children.map(c => c.textContent || c.innerHTML)')),
        ['Welcome', 'Earlier status', '> Hello', '<em>Thinking... 0.0s</em>']);
    run(`handleServerMessage({type:'stream_chunk', content:'Reply'});
        handleServerMessage({type:'stream_end'});
        handleServerMessage({type:'submission_done', accepted:true, content:[
            {role:'user', content:'Hello'}, {role:'assistant', content:'Reply'}]});`);
    assert.deepEqual(Array.from(run('history.children.map(c => c.textContent || c.innerHTML)')),
        ['Welcome', 'Earlier status', '> Hello', 'Reply']);
});

test('successful send clears only the submitted draft and attachments', () => {
    const run = client();
    run("input.value='question'; pendingImages.push({data_url:'first'}); sendMessage();");
    assert.equal(run('input.value'), '');
    assert.equal(run('pendingImages.length'), 0);
    run(`input.value='next draft'; pendingImages.push({data_url:'second'});
        handleServerMessage({type:'submission_done', content:${exchange}, accepted:true});`);
    assert.equal(run('input.value'), 'next draft');
    assert.equal(run('pendingImages.length'), 1);
    assert.equal(run('pendingImages[0].data_url'), 'second');
});

test('disconnect mid-request restores the draft and attachments', () => {
    const run = client();
    run("input.value='question'; pendingImages.push({data_url:'first'}); sendMessage();");
    assert.equal(run('input.value'), '');
    run('ws.onclose();');
    assert.equal(run('input.value'), 'question');
    assert.equal(run('pendingImages.length'), 1);
    assert.equal(run('pendingSubmission'), null);
});

test('command send keeps attached images in the composer', () => {
    const run = client();
    run("input.value='/models'; pendingImages.push({data_url:'first'}); sendMessage();");
    assert.equal(run('input.value'), '');
    assert.equal(run('pendingImages.length'), 1);
});

test('failed submission keeps text typed while it was running', () => {
    const run = client();
    run(`input.value='question'; sendMessage(); input.value='follow-up';
        handleServerMessage({type:'submission_done', content:[], accepted:false});`);
    assert.equal(run('input.value'), 'question\n\nfollow-up');
});
