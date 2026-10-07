'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const { execFileSync } = require('node:child_process');
const { createSessionOperationQueue } = require('../src/session-lifecycle');
const { deadline } = require('../src/realtime-delivery');
const { patchSource } = require('../scripts/patch-hosted-session-lifecycle');

const gateway = path.resolve(__dirname, '..');
const work = fs.mkdtempSync(path.join(os.tmpdir(), 'hosted-lifecycle-tests-'));
fs.cpSync(path.join(gateway, 'src'), path.join(work, 'src'), { recursive: true });
const dockerfile = fs.readFileSync(path.join(gateway, 'Dockerfile'), 'utf8');
for (const match of dockerfile.matchAll(/^RUN node (scripts\/patch-hosted-\S+)/gm)) {
  execFileSync(process.execPath, [path.join(gateway, match[1])], { cwd: work });
}
const source = fs.readFileSync(path.join(work, 'src/index.js'), 'utf8');
fs.rmSync(work, { recursive: true, force: true });

function functionSource(name) {
  const start = source.search(new RegExp(`(?:async )?function ${name}\\(`));
  assert.ok(start >= 0, `Missing production function ${name}`);
  const tail = source.slice(start);
  const end = tail.search(/\n(?:async )?function /);
  return end < 0 ? tail : tail.slice(0, end);
}

function context(names, overrides = {}) {
  const sandbox = {
    console: { warn() {}, log() {}, error() {} }, fs, path,
    shuttingDown: false, sessions: new Map(), sessionFailureTrackers: new Map(),
    withSessionOperation: createSessionOperationQueue(), deadline,
    AUTH_PATH: '/test/auth', MAX_HOSTED_SESSIONS: 50, BASE_RETRY_MS: 30000,
    MAX_RETRY_MS: 1800000, MAX_RESTORE_ATTEMPTS: 6, QR_IDLE_TIMEOUT_MS: 120000,
    gatewayMetrics: { sessionsCreated: 0, reconnects: 0 },
    activeSessionCount: () => 0,
    process: { env: {} }, Date,
    digits: value => String(value || '').replace(/\D/g, ''),
    acquireLock: async () => true, releaseLock: async () => {},
    reconcileClientState: async () => {}, wireClientEvents() {},
    destroyClientBounded: async (_id, state) => { state.retired = true; },
    callback: async () => true, startHistorySync: async () => {},
    clearTimeout() {}, setTimeout: () => ({ unref() {} }),
    LocalAuth: class {},
    Client: class { initialize() { return Promise.resolve(); } },
    ...overrides,
  };
  vm.createContext(sandbox);
  vm.runInContext(names.map(functionSource).join('\n'), sandbox);
  return sandbox;
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function eventsContext(overrides = {}) {
  const handlers = {};
  const state = { status: 'initializing', client: { on: (event, handler) => { handlers[event] = handler; } } };
  const ctx = context(['wireClientEvents'], {
    QRCode: { toDataURL: async value => 'encoded:' + value },
    ...overrides,
  });
  ctx.sessions.set('account', state);
  ctx.wireClientEvents('account', state);
  return { ctx, state, handlers };
}

test('lifecycle patch is idempotent and rejects source drift', () => {
  assert.equal(patchSource(source), source);
  assert.throws(() => patchSource('unknown gateway'), /expected 1 matches/);
});

test('parallel creates launch one browser while unrelated accounts remain independent', async () => {
  const gate = deferred();
  let initialized = 0;
  const ctx = context(['createSession', 'createSessionUnlocked'], {
    acquireLock: async id => { if (id === 'account') await gate.promise; return true; },
    Client: class { initialize() { initialized += 1; return Promise.resolve(); } },
  });
  const first = ctx.createSession('account', '+919876543210');
  const second = ctx.createSession('account', '+919876543210');
  await ctx.createSession('other', '+919876543211');
  assert.equal(initialized, 1);
  gate.resolve();
  const [a, b] = await Promise.all([first, second]);
  assert.equal(a, b);
  assert.equal(initialized, 2);
});

test('failed session operation does not block a later explicit retry', async () => {
  const run = createSessionOperationQueue();
  const failed = run('account', async () => { throw new Error('temporary'); });
  const retry = run('account', async () => 'connected');
  await assert.rejects(failed, /temporary/);
  assert.equal(await retry, 'connected');
});

test('shutdown during Redis acquisition cannot launch a browser after shutdown', async () => {
  const gate = deferred();
  const entered = deferred();
  let released = 0;
  let launched = 0;
  const ctx = context(['createSession', 'createSessionUnlocked'], {
    acquireLock: async () => { entered.resolve(); await gate.promise; return true; },
    releaseLock: async () => { released += 1; },
    Client: class { initialize() { launched += 1; return Promise.resolve(); } },
  });
  const pending = ctx.createSession('account', '+919876543210');
  await entered.promise;
  ctx.shuttingDown = true;
  gate.resolve();
  await assert.rejects(pending, /shutting down/);
  assert.equal(launched, 0);
  assert.equal(released, 1);
});

test('initialization rejection from a replaced browser cannot release its successor lease', async () => {
  const startup = deferred();
  const released = [];
  const callbacks = [];
  const ctx = context(['createSession', 'createSessionUnlocked'], {
    Client: class { initialize() { return startup.promise; } },
    releaseLock: async id => released.push(id),
    callback: async (...args) => { callbacks.push(args); },
  });
  const old = await ctx.createSession('account', '+919876543210');
  const replacement = { status: 'running' };
  ctx.sessions.set('account', replacement);
  startup.reject(new Error('Target closed during refresh'));
  await Promise.resolve();
  await ctx.withSessionOperation('account', async () => {});
  assert.deepEqual(released, []);
  assert.deepEqual(callbacks, []);
  assert.equal(ctx.sessions.get('account'), replacement);
  assert.equal(old.status, 'initializing');
});

for (const status of ['failed', 'disconnected', 'expired']) {
  test(`explicit connect replaces a ${status} browser instead of returning its dead state`, async () => {
    const order = [];
    const ctx = context(['createSession', 'createSessionUnlocked'], {
      destroyClientBounded: async () => { order.push('destroy'); },
      releaseLock: async () => { order.push('release'); },
      acquireLock: async () => { order.push('acquire'); return true; },
    });
    const old = { status };
    ctx.sessions.set('account', old);
    const replacement = await ctx.createSession('account', '+919876543210');
    assert.notEqual(replacement, old);
    assert.equal(replacement.status, 'initializing');
    assert.deepEqual(order, ['destroy', 'release', 'acquire']);
  });
}

test('QR encoding completed after authentication cannot revert the session to QR-ready', async () => {
  const encoding = deferred();
  const callbacks = [];
  const { state, handlers } = eventsContext({
    QRCode: { toDataURL: () => encoding.promise },
    callback: async (_id, event) => { callbacks.push(event); },
  });
  const qr = handlers.qr('raw');
  await handlers.authenticated();
  encoding.resolve('stale-qr');
  await qr;
  assert.equal(state.status, 'connecting');
  assert.equal(state.qr, null);
  assert.deepEqual(callbacks, ['authenticated']);
});

test('out-of-order QR encoding retains only the newest provider QR', async () => {
  const old = deferred();
  const fresh = deferred();
  const { state, handlers } = eventsContext({
    QRCode: { toDataURL: value => value === 'old' ? old.promise : fresh.promise },
  });
  const a = handlers.qr('old');
  const b = handlers.qr('new');
  fresh.resolve('fresh-image');
  await b;
  old.resolve('stale-image');
  await a;
  assert.equal(state.qr, 'fresh-image');
  assert.equal(state.status, 'qr_ready');
});

test('QR encoder errors become recoverable failures instead of unhandled async rejections', async () => {
  const { state, handlers } = eventsContext({
    QRCode: { toDataURL: async () => { throw new Error('encode failed'); } },
  });
  await handlers.qr('raw');
  assert.equal(state.status, 'failed');
  assert.equal(state.qr, null);
  assert.match(state.lastError, /encode failed/);
  assert.ok(state.restoreRetryAt > Date.now());
});

test('CONNECTED before ClientInfo waits for identity before announcing ready', async () => {
  const callbacks = [];
  const ctx = context(['promoteRunningSession'], {
    callback: async (_id, event) => { callbacks.push(event); },
  });
  const state = { status: 'connecting', requestedPhone: '+919876543210', client: {} };
  ctx.sessions.set('account', state);
  assert.equal(await ctx.promoteRunningSession('account', state, 'state_probe'), false);
  assert.equal(state.status, 'connecting');
  assert.deepEqual(callbacks, []);
  state.client.info = { wid: { user: '919876543210' } };
  assert.equal(await ctx.promoteRunningSession('account', state, 'ready_event'), true);
  assert.equal(state.status, 'running');
  assert.deepEqual(callbacks, ['ready']);
});

test('a mismatched linked number still fails even after an early CONNECTED event', async () => {
  let loggedOut = 0;
  const ctx = context(['promoteRunningSession']);
  const state = {
    status: 'connecting', requestedPhone: '+919876543210',
    client: { logout: async () => { loggedOut += 1; } },
  };
  ctx.sessions.set('account', state);
  await ctx.promoteRunningSession('account', state, 'state_probe');
  state.client.info = { wid: { user: '919876543211' } };
  assert.equal(await ctx.promoteRunningSession('account', state, 'ready_event'), false);
  assert.equal(state.status, 'failed');
  assert.equal(loggedOut, 1);
});

test('status reconciliation schedules recovery without destructive disconnected callbacks', async () => {
  const callbacks = [];
  const ctx = context(['reconcileClientState'], {
    callback: async (_id, event) => { callbacks.push(event); },
  });
  const state = { status: 'running', callbackReady: true, client: { getState: async () => 'UNPAIRED' } };
  ctx.sessions.set('account', state);
  await ctx.reconcileClientState('account', state);
  assert.equal(state.status, 'connecting');
  assert.equal(state.reconnectRequired, true);
  assert.equal(state.callbackReady, false);
  assert.ok(state.restoreRetryAt > Date.now());
  assert.deepEqual(callbacks, ['connecting']);
});

test('a slow connection probe preserves inbox history by using a connecting callback', async () => {
  const callbacks = [];
  const ctx = context(['reconcileClientState'], {
    deadline: async () => { throw new Error('Hosted session probe timed out'); },
    callback: async (_id, event) => { callbacks.push(event); },
  });
  const state = { status: 'running', client: {} };
  ctx.sessions.set('account', state);
  await ctx.reconcileClientState('account', state);
  assert.equal(state.status, 'connecting');
  assert.equal(state.reconnectRequired, true);
  assert.deepEqual(callbacks, ['connecting']);
});

test('a changed expected phone cannot retain an already-running browser for the wrong number', async () => {
  const callbacks = [];
  let loggedOut = false;
  const ctx = context(['promoteRunningSession'], {
    callback: async (_id, event) => { callbacks.push(event); },
  });
  const state = {
    status: 'running', requestedPhone: '+919876543210',
    client: { info: { wid: { user: '919876543211' } }, logout: async () => { loggedOut = true; } },
  };
  ctx.sessions.set('account', state);
  assert.equal(await ctx.promoteRunningSession('account', state, 'state_probe'), false);
  assert.equal(state.status, 'failed');
  assert.equal(loggedOut, true);
  assert.deepEqual(callbacks, ['failed']);
});

test('stalled startup becomes retryable instead of staying connecting indefinitely', async () => {
  const ctx = context(['reconcileClientState']);
  const state = {
    status: 'connecting', startupStartedAt: Date.now() - 180000,
    client: { getState: async () => null },
  };
  ctx.sessions.set('account', state);
  await ctx.reconcileClientState('account', state);
  assert.equal(state.status, 'failed');
  assert.match(state.lastError, /initialization timed out/);
  assert.ok(state.restoreRetryAt > Date.now());
});

test('abandoned QR tombstones do not consume browser capacity forever', async () => {
  const ctx = context(['activeSessionCount', 'createSession', 'createSessionUnlocked'], {
    MAX_HOSTED_SESSIONS: 1,
  });
  ctx.sessions.set('abandoned', { status: 'expired' });
  const active = await ctx.createSession('account', '+919876543210');
  assert.equal(active.status, 'initializing');
  await assert.rejects(ctx.createSession('other', '+919876543211'), /capacity reached/);
});

test('a delayed connection probe cannot change a replacement session', async () => {
  const probe = deferred();
  const callbacks = [];
  const ctx = context(['reconcileClientState'], {
    callback: async (...args) => { callbacks.push(args); },
  });
  const old = { status: 'running', client: { getState: () => probe.promise } };
  ctx.sessions.set('account', old);
  const running = ctx.reconcileClientState('account', old);
  ctx.sessions.set('account', { status: 'running' });
  probe.resolve('UNPAIRED');
  await running;
  assert.equal(old.status, 'running');
  assert.deepEqual(callbacks, []);
});

test('restore snapshot cannot recreate a profile removed by logout while queued', async () => {
  const gate = deferred();
  let creates = 0;
  let profileExists = true;
  const ctx = context(['restoreSessions'], {
    fs: { promises: {
      readdir: async () => [{ isDirectory: () => true, name: 'session-account' }],
      access: async () => { if (!profileExists) throw new Error('ENOENT'); },
    } },
    sessionProfilePath: id => '/test/' + id,
    createSessionUnlocked: async () => { creates += 1; },
  });
  const logout = ctx.withSessionOperation('account', async () => {
    await gate.promise;
    profileExists = false;
  });
  const scan = ctx.restoreSessions();
  await Promise.resolve();
  gate.resolve();
  await Promise.all([logout, scan]);
  assert.equal(creates, 0);
});

test('expired QR cleanup queued behind a refresh never deletes the replacement profile', async () => {
  const gate = deferred();
  let deleted = 0;
  const ctx = context(['expireIdleQrSessions'], {
    removeSessionProfile: async () => { deleted += 1; },
  });
  const old = { status: 'qr_ready', qrIdleStartedAt: Date.now() - 600000 };
  ctx.sessions.set('account', old);
  const refresh = ctx.withSessionOperation('account', async () => {
    await gate.promise;
    ctx.sessions.set('account', { status: 'connecting' });
  });
  const expiry = ctx.expireIdleQrSessions();
  gate.resolve();
  await Promise.all([refresh, expiry]);
  assert.equal(deleted, 0);
  assert.equal(ctx.sessions.get('account').status, 'connecting');
});

test('lease fencing queued behind refresh cannot disconnect its replacement', async () => {
  const gate = deferred();
  const callbacks = [];
  const ctx = context(['fenceSession', 'fenceSessionUnlocked'], {
    callback: async (...args) => { callbacks.push(args); },
  });
  const old = { status: 'running', client: {} };
  const replacement = { status: 'running', client: {} };
  ctx.sessions.set('account', old);
  const refresh = ctx.withSessionOperation('account', async () => {
    await gate.promise;
    ctx.sessions.set('account', replacement);
  });
  const fence = ctx.fenceSession('account', old, 'lease_lost');
  gate.resolve();
  await Promise.all([refresh, fence]);
  assert.equal(ctx.sessions.get('account'), replacement);
  assert.deepEqual(callbacks, []);
});

test('QR endpoint hides an image whose advertised lifetime has elapsed', async () => {
  const start = source.indexOf("app.get('/sessions/:sessionId/qr',");
  const end = source.indexOf('\n});', start);
  let handler;
  let result;
  const state = { qr: 'stale-image', qrGeneratedAt: Date.now() - 65000 };
  vm.runInNewContext(source.slice(start, end + 4), {
    app: { get: (_route, callback) => { handler = callback; } },
    sessions: new Map([['account', state]]),
    reconcileClientState: async () => {}, publicSession: () => ({}),
    QR_EXPIRES_SECONDS: 60, Date,
  });
  await handler({ params: { sessionId: 'account' } }, { json: value => { result = value; } });
  assert.equal(result.qr, null);
  assert.equal(result.expiresIn, 0);
});

test('slow destructive logout keeps its lease until profile deletion and blocks successor creation', async () => {
  const logout = deferred();
  const entered = deferred();
  const order = [];
  const ctx = context(['logoutSession', 'logoutSessionUnlocked'], {
    sessionProfilePath: () => '/test/profile',
    removeSessionProfile: async () => { order.push('remove'); },
    releaseLock: async () => { order.push('release'); },
  });
  const state = { status: 'running', client: {
    logout: async () => { entered.resolve(); await logout.promise; order.push('provider-logout'); },
  } };
  ctx.sessions.set('account', state);
  const pending = ctx.logoutSession('account');
  const successor = ctx.withSessionOperation('account', async () => { order.push('successor'); });
  await entered.promise;
  assert.equal(ctx.sessions.get('account'), state, 'lease renewer retains the retiring owner');
  assert.equal(state.status, 'disconnecting');
  assert.deepEqual(order, []);
  logout.resolve();
  await Promise.all([pending, successor]);
  assert.deepEqual(order, ['provider-logout', 'remove', 'release', 'successor']);
});

test('orphan logout cannot delete a profile leased by another gateway', async () => {
  let deleted = false;
  const ctx = context(['logoutSession', 'logoutSessionUnlocked'], {
    sessionProfilePath: () => '/test/profile',
    acquireLock: async () => false,
    removeSessionProfile: async () => { deleted = true; },
  });
  await assert.rejects(ctx.logoutSession('account'), /another gateway/);
  assert.equal(deleted, false);
});
