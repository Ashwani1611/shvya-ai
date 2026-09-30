const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const script = fs.readFileSync(path.join(__dirname, '../../static/js/instagram_inbox.js'), 'utf8');

function harness({ media = [], fetchReply } = {}) {
    const nodes = new Map(), listeners = new Map(), timers = new Map(), intervals = new Map(), requests = [];
    let sequence = 0;
    const makeNode = () => ({
        dataset: {}, textContent: '', value: '', hidden: false, disabled: false, style: {}, children: [],
        scrollHeight: 500, scrollTop: 0, clientHeight: 500,
        classList: { toggle() {} },
        events: new Map(),
        addEventListener(event, listener) { this.events.set(event, listener); },
        querySelectorAll(selector) { return selector === 'img,video,audio' ? media : []; },
        querySelector() { return null; },
        setAttribute() {}, removeAttribute() {},
        replaceChildren(...children) { this.children = children; },
        append(child) { this.children.push(child); },
    });
    const node = id => {
        if (!nodes.has(id)) nodes.set(id, makeNode());
        return nodes.get(id);
    };
    node('instagram-initial').textContent = JSON.stringify({ active_conversation: null, next_offset: null, list_url: '/dashboard/instagram/chats/' });
    node('instagram-inbox-shell').querySelector = selector => selector === '.wa-chat-surface' ? node('surface') : null;
    node('composer-form').querySelector = () => node('submit');
    node('ig-conversations').parentElement = node('list-parent');
    const context = {
        document: { hidden: false, getElementById: node, createElement: makeNode, addEventListener: (event, listener) => listeners.set(event, listener) },
        window: {}, navigator: { onLine: true },
        location: { origin: 'https://shvya.test', href: 'https://shvya.test/dashboard/instagram/chats/', pathname: '/dashboard/instagram/chats/' },
        history: { pushState() {} },
        URL, Date, AbortController, AbortSignal,
        crypto: { randomUUID: () => 'one-test-message-id' },
        addEventListener: (event, listener) => listeners.set(event, listener),
        setTimeout: (fn, wait) => { const id = ++sequence; timers.set(id, { fn, wait }); return id; },
        clearTimeout: id => timers.delete(id),
        setInterval: (fn, wait) => { const id = ++sequence; intervals.set(id, { fn, wait }); return id; },
        clearInterval: id => intervals.delete(id),
        requestAnimationFrame: fn => fn(),
        fetch: async (url, options) => {
            requests.push({ url, options });
            if (fetchReply) return fetchReply(url, options);
            return { ok: true, redirected: false, headers: { get: () => 'application/json' }, json: async () => ({ conversations: [], active_conversation: null, next_offset: null, instagram_inbox_ready: true }) };
        },
    };
    vm.runInNewContext(script, context);
    return {
        node, timers, intervals, requests,
        emit: (event, payload = {}) => listeners.get(event)?.(payload),
        runTimer(wait) {
            const entry = [...timers.entries()].find(([, value]) => value.wait === wait);
            assert.ok(entry, `Expected timer after ${wait}ms`);
            timers.delete(entry[0]);
            return entry[1].fn();
        },
    };
}

test('inbox polling and reply-window checks resume after browser history restoration without losing drafts', async () => {
    const h = harness();
    h.node('message-body').value = 'Keep my unsent draft';
    assert.equal(h.intervals.size, 1);
    h.emit('pagehide');
    assert.equal(h.intervals.size, 0);
    assert.equal(h.timers.size, 0);
    h.emit('pageshow', { persisted: true });
    assert.equal(h.intervals.size, 1);
    await h.runTimer(4000);
    assert.equal(h.requests.length, 1);
    assert.equal(h.node('message-body').value, 'Keep my unsent draft');
    h.emit('pagehide');
    h.emit('pageshow', { persisted: true });
    assert.equal(h.intervals.size, 1);
    await h.runTimer(4000);
    assert.equal(h.requests.length, 2);
});

test('media already failed before deferred initialization still shows its unavailable fallback', () => {
    const fallback = { hidden: true };
    const image = {
        dataset: {}, tagName: 'IMG', complete: true, naturalWidth: 0,
        addEventListener() {},
        closest: () => ({ querySelector: () => fallback }),
    };
    harness({ media: [image] });
    assert.equal(fallback.hidden, false);
});

test('inbox timeouts show retry feedback and back off instead of silently remaining stuck', async () => {
    const h = harness({ fetchReply: (_url, { signal }) => new Promise((resolve, reject) => {
        signal.addEventListener('abort', () => {
            const error = new Error('Aborted'); error.name = 'AbortError'; reject(error);
        });
    }) });
    const request = h.runTimer(4000);
    h.runTimer(12000);
    await request;
    assert.match(h.node('ig-live-state').textContent, /taking longer than expected/);
    assert.ok([...h.timers.values()].some(timer => timer.wait === 8000));
});
