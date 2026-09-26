'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const { createHostedLifecycle, hasExited } = require('../src/session-lifecycle');

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
async function until(predicate, ms = 2000) {
  const deadline = Date.now() + ms;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error('Condition did not become true');
    await new Promise(resolve => setTimeout(resolve, 2));
  }
}
function harness(t, settings = {}) {
  const sessions = new Map();
  const clients = [];
  const actions = [];
  const failures = [];
  let clock = 100;
  class FakeClient extends EventEmitter {
    constructor(id) {
      super();
      this.id = id;
      this.child = Object.assign(new EventEmitter(), {
        pid: 2000000000 + clients.length, exitCode: null, signalCode: null, killed: false,
      });
      this.child.kill = signal => {
        actions.push(`${id}:${signal}`);
        this.child.killed = true;
        if (signal === 'SIGKILL' || settings.termExits) this.exit(null, signal);
      };
      this.authStrategy = { logout: async () => {
        actions.push(`${id}:profile`);
        assert.ok(!this.pupBrowser || hasExited(this.child), 'profile deletion preceded browser exit');
      } };
      clients.push(this);
    }
    exit(code = 0, signal = null) {
      if (hasExited(this.child)) return;
      this.child.exitCode = code;
      this.child.signalCode = signal;
      this.child.emit('exit', code, signal);
    }
    async initialize() {
      actions.push(`${this.id}:initialize`);
      if (settings.initialize) return settings.initialize(this);
      this.pupBrowser = { process: () => this.child };
    }
    async destroy() {
      actions.push(`${this.id}:destroy`);
      if (settings.destroy) return settings.destroy(this);
      this.exit();
    }
    async logout() {
      actions.push(`${this.id}:logout`);
      if (settings.logout) return settings.logout(this);
    }
  }
  const lifecycle = createHostedLifecycle({
    sessions, createClient: id => new FakeClient(id), maxSessions: settings.maxSessions || 50,
    wireClientEvents: (id, state) => {
      state.client.on('auth_failure', () => { throw new Error('Unmanaged auth callback'); });
      state.client.on('disconnected', () => { throw new Error('Unmanaged disconnect callback'); });
      state.client.on('ready', () => { actions.push(`${id}:ready`); state.status = 'running'; });
      settings.wire?.(id, state);
    },
    reconcileClientState: async () => {},
    acquireLock: async id => {
      actions.push(`${id}:acquire`);
      return settings.acquire ? settings.acquire(id) : true;
    },
    releaseLock: async id => { actions.push(`${id}:release`); },
    callback: async (id, event) => {
      actions.push(`${id}:callback:${event}`);
      return settings.callback ? settings.callback(id, event) : true;
    },
    authPath: '/unused-test-path',
    readProfiles: async () => (settings.profiles || []).map(id => ({ name: `session-${id}`, isDirectory: () => true })),
    now: () => clock, random: () => 0,
    log: { warn() {}, error() {}, log() {} },
    onUnsafeCleanup: e => failures.push(e),
    processAlive: record => !hasExited(record.child),
    signalChild: (record, signal) => record.child.kill(signal),
    timings: { close: 10, term: 10, kill: 30, poll: 2, redis: 20,
      logout: 10, retryBase: 10, retryMax: 1000, retryLimit: 3, stable: 1000,
      ...settings.timings },
  });
  t.after(() => lifecycle.shutdown().catch(() => {}));
  return { lifecycle, sessions, clients, actions, failures, setClock: value => { clock = value; } };
}

module.exports = function registerLifecycleTests({ source, root } = {}) {
  test('concurrent creates share one client and one lease acquisition', async t => {
    const gate = deferred();
    const h = harness(t, { acquire: () => gate.promise });
    const first = h.lifecycle.createSession('one', '+919811223344');
    const second = h.lifecycle.createSession('one', '+919811223344');
    await until(() => h.actions.includes('one:acquire'));
    assert.equal(h.clients.length, 0);
    gate.resolve(true);
    assert.equal(await first, await second);
    assert.equal(h.clients.length, 1);
    assert.equal(h.actions.filter(x => x === 'one:acquire').length, 1);
  });

  test('in-flight reservations enforce capacity across different session IDs', async t => {
    const gate = deferred();
    const h = harness(t, { maxSessions: 1, acquire: () => gate.promise });
    const first = h.lifecycle.createSession('one');
    await until(() => h.actions.includes('one:acquire'));
    await assert.rejects(h.lifecycle.createSession('two'), /capacity/);
    gate.resolve(true);
    await first;
    assert.equal(h.clients.length, 1);
  });

  test('refresh destroys the old browser before releasing its lease or starting replacement', async t => {
    const h = harness(t);
    const old = await h.lifecycle.createSession('one', '+919811223344');
    await until(() => old.initSettled);
    const replacement = await h.lifecycle.refreshQr('one');
    assert.notEqual(old, replacement);
    assert.ok(old.stopped);
    assert.ok(h.actions.indexOf('one:destroy') < h.actions.indexOf('one:release'));
    assert.equal(h.clients.length, 2);
    assert.equal(replacement.requestedPhone, '+919811223344');
    assert.ok(!h.actions.includes('one:profile'));
  });

  test('destruction is idempotent and a sent signal is not treated as process exit', async t => {
    const h = harness(t, { destroy: () => new Promise(() => {}) });
    const state = await h.lifecycle.createSession('one');
    await until(() => state.initSettled);
    const first = h.lifecycle.stopClient('one', state);
    assert.equal(first, h.lifecycle.stopClient('one', state));
    await first;
    assert.ok(h.actions.includes('one:SIGTERM'));
    assert.ok(h.actions.includes('one:SIGKILL'));
    assert.equal(h.actions.filter(x => x === 'one:destroy').length, 1);
    assert.ok(hasExited(state.client.child));
  });

  test('a browser assigned after teardown begins is still closed before replacement', async t => {
    const gate = deferred();
    let calls = 0;
    const h = harness(t, { initialize: async client => {
      if (++calls === 1) await gate.promise;
      client.pupBrowser = { process: () => client.child };
    } });
    const old = await h.lifecycle.createSession('one');
    const replacement = h.lifecycle.refreshQr('one');
    await new Promise(resolve => setTimeout(resolve, 5));
    assert.equal(h.clients.length, 1);
    assert.ok(!h.actions.includes('one:release'));
    gate.resolve();
    await replacement;
    assert.ok(old.stopped);
    assert.equal(h.clients.length, 2);
  });

  test('unsettled initialization fails closed without releasing or reusing the profile', async t => {
    const h = harness(t, { initialize: () => new Promise(() => {}) });
    await h.lifecycle.createSession('one');
    await assert.rejects(h.lifecycle.refreshQr('one'), /cleanup could not be confirmed/);
    assert.equal(h.clients.length, 1);
    assert.equal(h.failures.length, 1);
    assert.ok(!h.actions.includes('one:release'));
    await assert.rejects(h.lifecycle.createSession('two'), /shutting down/);
  });

  test('duplicate disconnect events clean up once and stale callbacks cannot revive a session', async t => {
    const h = harness(t);
    const old = await h.lifecycle.createSession('one');
    await until(() => old.initSettled);
    old.client.emit('disconnected', 'network');
    old.client.emit('disconnected', 'network');
    await until(() => old.restoreRetryAt > 0);
    assert.equal(h.actions.filter(x => x === 'one:destroy').length, 1);
    old.client.emit('ready');
    await new Promise(resolve => setImmediate(resolve));
    assert.notEqual(old.status, 'running');
    assert.ok(!h.actions.includes('one:ready'));
  });

  test('status callbacks remain serialized with cleanup and replacement', async t => {
    const gate = deferred();
    const h = harness(t, { callback: () => gate.promise });
    const old = await h.lifecycle.createSession('one');
    await until(() => old.initSettled);
    old.client.emit('disconnected', 'network');
    const next = h.lifecycle.refreshQr('one');
    await until(() => old.stopped);
    assert.equal(h.clients.length, 1);
    assert.ok(!h.actions.includes('one:release'));
    gate.resolve(true);
    await next;
    assert.equal(h.clients.length, 2);
  });

  test('initialization retries back off and pause at the consecutive failure limit', async t => {
    const h = harness(t, { profiles: ['one'], initialize: async () => { throw new Error('launch failed'); } });
    let state = await h.lifecycle.createSession('one');
    await until(() => state.restoreRetryAt > 0);
    assert.equal(state.restoreRetryAt, 110);
    await h.lifecycle.restoreSessions();
    assert.equal(h.clients.length, 1);
    h.setClock(110);
    await h.lifecycle.restoreSessions();
    state = h.sessions.get('one');
    await until(() => state.restoreRetryAt > 0);
    assert.equal(state.restoreRetryAt, 130);
    h.setClock(130);
    await h.lifecycle.restoreSessions();
    state = h.sessions.get('one');
    await until(() => state.retryPaused);
    h.setClock(100000);
    await h.lifecycle.restoreSessions();
    assert.equal(h.clients.length, 3);
    assert.equal(h.lifecycle.diagnostics().retryPaused, 1);
    await h.lifecycle.refreshQr('one');
    assert.equal(h.clients.length, 4);
  });

  test('concurrent restore scans are single-flight', async t => {
    const gate = deferred();
    const h = harness(t, { profiles: ['one'], acquire: () => gate.promise });
    const first = h.lifecycle.restoreSessions();
    const second = h.lifecycle.restoreSessions();
    assert.equal(first, second);
    gate.resolve(true);
    await first;
    assert.equal(h.clients.length, 1);
  });

  test('logout clears LocalAuth only after browser exit and a stale restore scan cannot resurrect it', async t => {
    const h = harness(t, { profiles: ['one'] });
    const state = await h.lifecycle.createSession('one');
    await until(() => state.initSettled);
    await h.lifecycle.logoutSession('one');
    assert.ok(h.actions.indexOf('one:destroy') < h.actions.indexOf('one:profile'));
    assert.ok(h.actions.indexOf('one:profile') < h.actions.indexOf('one:release'));
    await h.lifecycle.restoreSessions();
    assert.equal(h.clients.length, 1);
    assert.equal(h.sessions.size, 0);
  });

  test('unknown-session logout never releases another generation\'s lease', async t => {
    const h = harness(t);
    await h.lifecycle.logoutSession('unknown');
    assert.equal(h.actions.length, 0);
  });

  test('timed-out logout cannot later delete a replacement profile', async t => {
    const h = harness(t, { logout: () => new Promise(() => {}) });
    const state = await h.lifecycle.createSession('one');
    await until(() => state.initSettled);
    await assert.rejects(h.lifecycle.logoutSession('one'), /logout timed out/);
    await assert.rejects(h.lifecycle.refreshQr('one'), /shutting down/);
    assert.ok(!h.actions.includes('one:release'));
    assert.equal(h.clients.length, 1);
  });

  test('idle QR expiry stops the browser before deleting unpaired LocalAuth', async t => {
    const h = harness(t);
    const state = await h.lifecycle.createSession('one');
    await until(() => state.initSettled);
    state.status = 'qr_ready';
    state.qrIdleStartedAt = 1;
    await h.lifecycle.expireIdleQrSessions(50);
    assert.equal(state.status, 'expired');
    assert.ok(state.stopped);
    assert.ok(h.actions.indexOf('one:destroy') < h.actions.indexOf('one:profile'));
    assert.ok(h.actions.indexOf('one:profile') < h.actions.indexOf('one:release'));
  });

  test('foreign lease fencing destroys only the original client and never deletes the foreign lease', async t => {
    const h = harness(t);
    const state = await h.lifecycle.createSession('one');
    await until(() => state.initSettled);
    await h.lifecycle.fenceSession('one', state, 'lease_lost');
    assert.ok(state.stopped);
    assert.ok(!h.actions.includes('one:release'));
    assert.ok(state.restoreRetryAt > 0);
  });

  test('a stale generation cannot fence its replacement', async t => {
    const h = harness(t);
    const old = await h.lifecycle.createSession('one');
    await until(() => old.initSettled);
    const next = await h.lifecycle.refreshQr('one');
    await h.lifecycle.fenceSession('one', old, 'old-lease');
    assert.equal(h.sessions.get('one'), next);
    assert.equal(next.closing, false);
  });

  test('shutdown is idempotent, rejects new work and preserves authenticated profiles', async t => {
    const h = harness(t);
    const state = await h.lifecycle.createSession('one');
    await until(() => state.initSettled);
    const first = h.lifecycle.shutdown();
    assert.equal(first, h.lifecycle.shutdown());
    await assert.rejects(h.lifecycle.createSession('two'), /shutting down/);
    await first;
    assert.equal(h.sessions.size, 0);
    assert.ok(!h.actions.includes('one:logout'));
    assert.ok(!h.actions.includes('one:profile'));
    assert.ok(h.actions.indexOf('one:destroy') < h.actions.indexOf('one:release'));
  });

  test('shutdown also drains a create waiting for Redis without launching a browser', async t => {
    const gate = deferred();
    const h = harness(t, { acquire: () => gate.promise });
    const creation = h.lifecycle.createSession('one');
    await until(() => h.actions.includes('one:acquire'));
    const stopping = h.lifecycle.shutdown();
    gate.resolve(true);
    await assert.rejects(creation, /shutting down/);
    await stopping;
    assert.equal(h.clients.length, 0);
    assert.ok(h.actions.includes('one:release'));
  });

  test('real owned child ignoring TERM is killed and its exit is confirmed', { timeout: 5000 }, async t => {
    const child = spawn(process.execPath, ['-e', "process.on('SIGTERM',()=>{}); console.log('ready'); setInterval(()=>{},1000);"], { stdio: ['ignore', 'pipe', 'pipe'] });
    t.after(() => { if (!hasExited(child)) child.kill('SIGKILL'); });
    await once(child.stdout, 'data');
    const sessions = new Map();
    const actions = [];
    const client = Object.assign(new EventEmitter(), {
      pupBrowser: { process: () => child },
      initialize: async () => {}, destroy: () => new Promise(() => {}),
    });
    const lifecycle = createHostedLifecycle({ sessions, maxSessions: 1,
      createClient: () => client, wireClientEvents() {}, reconcileClientState: async () => {},
      acquireLock: async () => true, releaseLock: async () => {
        assert.ok(hasExited(child)); actions.push('released');
      }, callback: async () => true, log: { warn() {} },
      timings: { close: 20, term: 20, kill: 500, poll: 5 },
    });
    await lifecycle.createSession('real-child');
    await lifecycle.shutdown();
    assert.equal(child.signalCode, 'SIGKILL');
    assert.deepEqual(actions, ['released']);
  });


  test('a surviving renderer is terminated even after its browser group root exits', { timeout: 5000, skip: process.platform !== 'linux' }, async t => {
    const script = `const {spawn}=require('node:child_process');
      const c=spawn(process.execPath,['-e',"process.on('SIGTERM',()=>{}); console.log('ready'); setInterval(()=>{},1000)"],{stdio:['ignore','pipe','ignore']});
      c.stdout.once('data',()=>console.log(c.pid)); setInterval(()=>{},1000);`;
    const child = spawn(process.execPath, ['-e', script], { detached: true, stdio: ['ignore', 'pipe', 'ignore'] });
    let renderer;
    let identity;
    function stat(pid) {
      try { const text = fs.readFileSync(`/proc/${pid}/stat`, 'utf8'); return text.slice(text.lastIndexOf(')') + 2).split(' '); }
      catch (_) { return null; }
    }
    t.after(() => {
      if (!hasExited(child)) child.kill('SIGKILL');
      if (renderer && stat(renderer)?.[19] === identity) {
        try { process.kill(renderer, 'SIGKILL'); } catch (_) {}
      }
    });
    renderer = Number(String((await once(child.stdout, 'data'))[0]).trim());
    identity = stat(renderer)[19];
    const client = Object.assign(new EventEmitter(), {
      pupBrowser: { process: () => child }, initialize: async () => {}, destroy: () => new Promise(() => {}),
    });
    const lifecycle = createHostedLifecycle({ sessions: new Map(), maxSessions: 1,
      createClient: () => client, wireClientEvents() {}, reconcileClientState: async () => {},
      acquireLock: async () => true, releaseLock: async () => {}, callback: async () => true,
      log: { warn() {} }, timings: { close: 20, term: 80, kill: 600, poll: 10 },
    });
    await lifecycle.createSession('real-group');
    await lifecycle.shutdown();
    assert.equal(child.signalCode, 'SIGTERM');
    const remaining = stat(renderer);
    assert.ok(!remaining || ['Z', 'X'].includes(remaining[0]), 'renderer survived cleanup');
  });

  test('an async event finishing after replacement cannot send its stale callback', async t => {
    const gate = deferred();
    let callbackPermitted;
    let entered = false;
    let h;
    h = harness(t, { wire: (id, state) => state.client.on('slow', async () => {
      entered = true;
      await gate.promise;
      callbackPermitted = h.lifecycle.callbackAllowed(id);
    }) });
    const old = await h.lifecycle.createSession('one');
    await until(() => old.initSettled);
    old.client.emit('slow');
    await until(() => entered);
    await h.lifecycle.refreshQr('one');
    gate.resolve();
    await until(() => callbackPermitted !== undefined);
    assert.equal(callbackPermitted, false);
  });

  if (source) {
    test('production adapters use the lifecycle owner and disable Puppeteer signal handlers', () => {
      assert.match(source, /hostedLifecycle\.createSession\(sessionId, requestedPhone\)/);
      assert.match(source, /hostedLifecycle\.shutdown\(\)/);
      assert.match(source, /handleSIGTERM: false/);
      assert.match(source, /handleSIGINT: false/);
      assert.match(source, /handleSIGHUP: false/);
      assert.match(source, /hostedLifecycle\.callbackAllowed\(sessionId\)/);
      assert.match(source, /res\.status\(shuttingDown \? 503 : 200\)/);
      new vm.Script(source);
    });
    test('production gateway boots with mocked channels, creates once and drains on SIGTERM', async t => {
      const auth = fs.mkdtempSync(path.join(os.tmpdir(), 'shvya-lifecycle-'));
      t.after(() => fs.rmSync(auth, { recursive: true, force: true }));
      const routes = new Map();
      const signals = new Map();
      const exits = [];
      const clients = [];
      const app = { disable() {}, use() {}, get: (url, handler) => routes.set(url, handler), post() {}, delete() {}, listen() {} };
      const express = Object.assign(() => app, { json: () => () => {}, raw: () => () => {} });
      class Client extends EventEmitter {
        constructor(options) {
          super(); this.options = options; this.authStrategy = options.authStrategy;
          this.child = Object.assign(new EventEmitter(), { pid: 2001000000, exitCode: null, signalCode: null });
          clients.push(this);
        }
        async initialize() { this.pupBrowser = { process: () => this.child }; }
        async getState() { return 'OPENING'; }
        async destroy() { this.child.exitCode = 0; this.child.emit('exit', 0, null); }
      }
      const sandbox = {
        require: name => {
          if (name === 'express') return express;
          if (name === 'qrcode') return { toDataURL: async () => 'qr' };
          if (name === 'redis') return { createClient() { throw new Error('Unexpected Redis connection'); } };
          if (name === 'whatsapp-web.js') return { Client, LocalAuth: class { async logout() { throw new Error('Shutdown must not logout'); } }, MessageMedia: {} };
          if (name === './session-lifecycle') return require('../src/session-lifecycle');
          if (name.startsWith('./')) return require(path.join(root, 'src', name));
          return require(name);
        },
        process: { env: { WHATSAPP_WEB_SESSION_PATH: auth }, on: (signal, handler) => signals.set(signal, handler),
          exit: code => exits.push(code), memoryUsage: process.memoryUsage, cpuUsage: process.cpuUsage, uptime: process.uptime },
        console: { log() {}, warn() {}, error() {} }, Buffer, URL, AbortSignal,
        setTimeout, clearTimeout, setInterval: () => ({ unref() {} }), clearInterval,
        fetch: async () => ({ ok: true }),
      };
      vm.runInNewContext(source + '\n;globalThis.controls = { createSession, shutdown };', sandbox);
      await new Promise(resolve => setImmediate(resolve));
      const [one, two] = await Promise.all([
        sandbox.controls.createSession('test-session', '+919811223344'),
        sandbox.controls.createSession('test-session', '+919811223344'),
      ]);
      assert.equal(one, two);
      assert.equal(clients.length, 1);
      assert.equal(clients[0].options.puppeteer.handleSIGTERM, false);
      await signals.get('SIGTERM')();
      assert.deepEqual(exits, [0]);
      assert.ok(hasExited(clients[0].child));
      let status, body;
      routes.get('/health')({}, { status: value => { status = value; return { json: value => { body = value; } }; } });
      assert.equal(status, 503);
      assert.equal(body.ok, false);
    });
    test('production Compose enables init without changing session persistence', () => {
      const compose = fs.readFileSync(path.join(root, '..', 'docker-compose.yml'), 'utf8');
      const gateway = compose.split('  whatsapp-web-gateway:')[1].split('\n\n  nginx:')[0];
      assert.match(gateway, /init: true/);
      assert.match(gateway, /stop_grace_period: 60s/);
      assert.match(gateway, /whatsapp_sessions:\/data\/wwebjs_auth/);
    });
  }
};

if (require.main === module) module.exports();
