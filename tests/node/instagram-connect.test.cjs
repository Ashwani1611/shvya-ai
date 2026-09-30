const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const script = fs.readFileSync(path.join(__dirname, '../../static/js/instagram_connect.js'), 'utf8');

function harness(state = 'authorizing', reply = {}) {
    const nodes = new Map();
    const node = id => {
        if (!nodes.has(id)) nodes.set(id, { textContent: '', hidden: id === 'ig-start-connect', classList: { add() {}, remove() {} } });
        return nodes.get(id);
    };
    const retry = { disabled: true };
    let now = 0, reloads = 0;
    const timers = new Map(), requests = [], replacements = [], listeners = new Map();
    let nextId = 0;
    const context = {
        document: {
            hidden: false,
            querySelector: () => ({ dataset: { igConnectState: state, statusUrl: '/dashboard/instagram/connect/' }, querySelectorAll: () => [retry] }),
            getElementById: node,
        },
        location: { hash: '#_', pathname: '/dashboard/instagram/connect/', search: '', reload: () => { reloads += 1; } },
        history: { state: null, replaceState: (...args) => replacements.push(args) },
        Date: { now: () => now },
        AbortController,
        setTimeout: (fn, wait) => { const id = ++nextId; timers.set(id, { fn, wait }); return id; },
        clearTimeout: id => timers.delete(id),
        addEventListener: (event, listener) => listeners.set(event, listener),
        fetch: async (url, options) => {
            requests.push({ url, options });
            if (reply instanceof Error) throw reply;
            return { ok: true, redirected: false, status: 200, headers: { get: () => 'application/json' }, json: async () => reply, ...reply.response };
        },
    };
    vm.runInNewContext(script, context);
    return {
        node, retry, requests, replacements, timers,
        emit: (event, payload = {}) => listeners.get(event)?.(payload),
        get reloads() { return reloads; },
        setReply(value) { reply = value; },
        setNow(value) { now = value; },
        async tick() {
            const entry = timers.entries().next().value;
            assert.ok(entry, 'Expected a scheduled connection check');
            timers.delete(entry[0]);
            await entry[1].fn();
        },
    };
}

test('authorization polling is read-only and reloads to show the completed connection', async () => {
    const h = harness('authorizing', { instagram_oauth_pending: false, instagram_connection_state: 'ready' });
    await h.tick();
    assert.equal(h.reloads, 1);
    assert.equal(h.requests[0].url, '/dashboard/instagram/connect/');
    assert.equal(h.requests[0].options.method, undefined);
    assert.equal(h.requests[0].options.credentials, 'same-origin');
    assert.equal(h.requests[0].options.cache, 'no-store');
    assert.equal(h.timers.size, 0);
});

test('account authorization transitions into the inbox setup screen, which keeps polling', async () => {
    const h = harness('authorizing', { instagram_oauth_pending: false, instagram_connection_state: 'authorized_with_warning' });
    await h.tick();
    assert.equal(h.reloads, 1);
    const setup = harness('authorized_with_warning', { instagram_oauth_pending: false, instagram_connection_state: 'authorized_with_warning', instagram_connection_error: 'Meta could not subscribe this account.' });
    await setup.tick();
    assert.equal(setup.reloads, 0);
    assert.equal(setup.node('ig-setup-error').textContent, 'Meta could not subscribe this account.');
    assert.equal(setup.node('ig-setup-error').hidden, false);
    setup.setReply({ instagram_oauth_pending: false, instagram_connection_state: 'ready' });
    await setup.tick();
    assert.equal(setup.reloads, 1);
});

test('temporary snapshot failures retain progress and recover without restarting login', async () => {
    const h = harness('authorizing', new Error('Network unavailable'));
    await h.tick();
    assert.match(h.node('ig-setup-poll').textContent, /temporarily unavailable/);
    assert.equal(h.reloads, 0);
    h.setReply({ instagram_oauth_pending: true, instagram_connection_state: 'authorizing' });
    await h.tick();
    assert.match(h.node('ig-setup-poll').textContent, /Checking your connection automatically/);
    assert.equal(h.requests.length, 2);
});

test('a session redirect stops polling and restores an actionable connect button', async () => {
    const h = harness('authorizing', { response: { redirected: true } });
    await h.tick();
    assert.match(h.node('ig-setup-poll').textContent, /session needs attention/);
    assert.equal(h.node('ig-start-connect').hidden, false);
    assert.equal(h.node('ig-pending-action').hidden, true);
    assert.equal(h.timers.size, 0);
    assert.equal(h.reloads, 0);
});

test('an authorization timeout shows recovery instead of leaving the action permanently disabled', async () => {
    const h = harness();
    h.setNow(660001);
    await h.tick();
    assert.match(h.node('ig-setup-state').textContent, /longer than expected/);
    assert.equal(h.node('ig-start-connect').hidden, false);
    assert.equal(h.retry.disabled, false);
    assert.equal(h.requests.length, 0);
});

test('Meta fragment cleanup does not poll or restart an already connected account', () => {
    const h = harness('ready');
    assert.equal(h.replacements.length, 1);
    assert.equal(h.replacements[0][2], '/dashboard/instagram/connect/');
    assert.equal(h.timers.size, 0);
    assert.equal(h.requests.length, 0);
});


test('restoring the connect screen from browser history resumes status checks repeatedly', async () => {
    const h = harness('authorizing', { instagram_oauth_pending: true, instagram_connection_state: 'authorizing' });
    h.emit('pagehide');
    assert.equal(h.timers.size, 0);
    h.emit('pageshow', { persisted: true });
    await h.tick();
    assert.equal(h.requests.length, 1);
    h.emit('pagehide');
    assert.equal(h.timers.size, 0);
    h.emit('pageshow', { persisted: true });
    await h.tick();
    assert.equal(h.requests.length, 2);
    assert.equal(h.reloads, 0);
});
