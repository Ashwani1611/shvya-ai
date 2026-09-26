'use strict';

const fs = require('node:fs');
const { AsyncLocalStorage } = require('node:async_hooks');

function bounded(promise, ms, label) {
  let timer;
  return Promise.race([
    Promise.resolve(promise),
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`${label} timed out`)), ms);
    }),
  ]).finally(() => clearTimeout(timer));
}

function hasExited(child) {
  // `killed` means a signal was sent, NOT that the process has exited.
  return child.exitCode != null || child.signalCode != null;
}

function processIdentity(pid) {
  try {
    const stat = fs.readFileSync(`/proc/${pid}/stat`, 'utf8');
    const fields = stat.slice(stat.lastIndexOf(')') + 2).split(' ');
    return { group: Number(fields[2]), started: fields[19], state: fields[0] };
  } catch (_) { return null; }
}

// Cache the proc snapshot across simultaneous session shutdowns. Never scan
// the whole process table once per session per polling tick.
let snapshotAt = 0;
let snapshot = [];
function groupSnapshot() {
  if (Date.now() - snapshotAt < 50) return snapshot;
  snapshotAt = Date.now();
  try {
    snapshot = fs.readdirSync('/proc').filter(name => /^\d+$/.test(name)).map(name => {
      const info = processIdentity(Number(name));
      return info ? { pid: Number(name), ...info } : null;
    }).filter(Boolean);
  } catch (_) { snapshot = []; }
  return snapshot;
}

function livingMembers(record) {
  if (!record.identity || record.identity.group !== record.child.pid) return [];
  const members = groupSnapshot().filter(info => info.group === record.child.pid && info.state !== 'Z' && info.state !== 'X');
  const known = record.members || new Map([[record.child.pid, record.identity.started]]);
  // At least one surviving identity must bridge consecutive snapshots. A
  // recycled PID/group is not authority to signal somebody else's processes.
  const owned = members.some(info => known.get(info.pid) === info.started);
  if (members.length && !owned) {
    record.ambiguousGroup = true;
    return [];
  }
  record.members = new Map(members.map(info => [info.pid, info.started]));
  return members;
}

function ownedProcessAlive(record) {
  const members = livingMembers(record);
  return !hasExited(record.child) || members.length > 0 || Boolean(record.ambiguousGroup);
}

function signalOwnedChild(record, signal) {
  const { child, identity } = record;
  const members = livingMembers(record);
  if (!Number.isSafeInteger(child.pid) || child.pid <= 1 || child.pid === process.pid) return;
  const current = processIdentity(child.pid);
  if (identity && current && identity.started !== current.started) return;
  // Signal the dedicated group only while its original root is still ours.
  if (!hasExited(child) && identity && current && identity.group === child.pid && current.group === child.pid) {
    try { process.kill(-child.pid, signal); return; } catch (error) {
      if (error.code !== 'ESRCH') throw error;
    }
  }
  if (!hasExited(child)) child.kill(signal);
  // The browser root may exit on TERM while a renderer survives. Escalate
  // only recorded, identity-checked members, including after root exit.
  for (const member of members) {
    if (member.pid <= 1 || member.pid === process.pid || member.pid === child.pid) continue;
    const info = processIdentity(member.pid);
    if (!info || info.started !== member.started || info.group !== child.pid) continue;
    try { process.kill(member.pid, signal); } catch (e) { if (e.code !== 'ESRCH') throw e; }
  }
}

/** Lifecycle owner for LocalAuth clients. No Redis key is released before stop. */
function createHostedLifecycle(options) {
  const {
    sessions, createClient, wireClientEvents, reconcileClientState,
    acquireLock, releaseLock, callback, authPath, maxSessions,
    metrics = {}, log = console, now = Date.now, random = Math.random,
    readProfiles = () => fs.promises.readdir(authPath, { withFileTypes: true }),
    signalChild = signalOwnedChild,
    processAlive = ownedProcessAlive,
    onUnsafeCleanup = () => {},
  } = options;
  const times = {
    close: 8000, term: 3000, kill: 3000, poll: 100,
    redis: 3000, logout: 5000, retryBase: 30000,
    retryMax: 15 * 60 * 1000, retryLimit: 6, stable: 60000,
    ...options.timings,
  };
  const operations = new Map();
  const reservations = new Set();
  const retries = new Map();
  const suppressedRestores = new Set();
  const events = new AsyncLocalStorage();
  let stopping = false;
  let restorePromise = null;
  let shutdownPromise = null;

  function unsafe(failure) {
    stopping = true;
    onUnsafeCleanup(failure);
  }
  function count(name) { metrics[name] = (metrics[name] || 0) + 1; }
  function error(message, statusCode = 503) { return Object.assign(new Error(message), { statusCode }); }
  function valid(id) {
    if (typeof id !== 'string' || !/^[-_\w]+$/.test(id)) throw error('Invalid session id.', 400);
  }
  function run(id, fn) {
    const result = (operations.get(id) || Promise.resolve()).then(fn);
    // The queue tail must never reject: a failed operation cannot poison the
    // next request, and callers still receive the original rejection.
    const tail = result.then(() => {}, () => {});
    operations.set(id, tail);
    tail.then(() => { if (operations.get(id) === tail) operations.delete(id); });
    return result;
  }
  function current(id, state) {
    return !stopping && sessions.get(id) === state && !state.closing && !state.stopped;
  }
  function callbackAllowed(id) {
    const owner = events.getStore();
    if (stopping) return false;
    return !owner || (owner.id === id && sessions.get(id) === owner.state &&
      (owner.terminal || current(id, owner.state)));
  }
  function notify(id, state, event, payload) {
    return events.run({ id, state, terminal: true }, () => callback(id, event, payload));
  }
  function background(promise) {
    Promise.resolve(promise).catch(e => log.warn('Hosted lifecycle operation failed:', e.message));
  }
  function activeCount() {
    return [...sessions.values()].filter(state => !state.stopped).length + reservations.size;
  }
  function retryFailure(id, state) {
    const previous = retries.get(id);
    const stable = state && state.runningSince != null && now() - state.runningSince >= times.stable;
    const failures = stable ? 1 : (previous?.failures || 0) + 1;
    const paused = failures >= times.retryLimit;
    const delay = Math.min(times.retryMax, times.retryBase * (2 ** Math.min(failures - 1, 10)));
    const retry = { failures, paused, at: paused ? 0 : now() + Math.ceil(delay * (1 + random() * 0.2)) };
    retries.set(id, retry);
    if (state) {
      state.restoreRetryAt = retry.at;
      state.retryPaused = paused;
    }
    if (paused) {
      count('retryPauses');
      log.warn(`Hosted session ${id} paused after ${failures} consecutive failures; refresh QR to retry.`);
    }
  }

  function observeBrowser(id, state) {
    const browser = state.client?.pupBrowser;
    if (!browser || typeof browser.process !== 'function') return;
    let child;
    try { child = browser.process(); } catch (_) { return; }
    if (!child || state.children.has(child)) return;
    const record = { child, identity: processIdentity(child.pid), browser };
    state.children.set(child, record);
    snapshotAt = 0;
    livingMembers(record);
    child.once('exit', (code, signal) => {
      snapshotAt = 0;
      count('browserExits');
      log.warn(`Hosted browser exit session=${id} pid=${child.pid} code=${code} signal=${signal || 'none'} closing=${state.closing}`);
      if (current(id, state)) background(failSession(id, state, 'disconnected', 'Chromium process exited', { retry: true }));
    });
  }

  function stopClient(id, state) {
    if (state.cleanupPromise) return state.cleanupPromise;
    state.closing = true;
    state.qr = null;
    state.qrGeneratedAt = 0;
    clearInterval(state.browserWatch);
    clearTimeout(state.historyRetryTimer);
    state.historyRetryTimer = null;
    state.cleanupPromise = (async () => {
      const started = Date.now();
      const attempted = new Set();
      let noBrowserDestroyAttempted = false;
      let destroyCompleted = false;
      for (;;) {
        observeBrowser(id, state);
        const browser = state.client?.pupBrowser;
        // initialize() can still be launching Chromium when shutdown starts.
        // Observe/close a browser assigned late; never call it clean merely
        // because pupBrowser was undefined on the first pass.
        if ((browser && !attempted.has(browser)) || (!browser && state.initSettled && !noBrowserDestroyAttempted)) {
          if (browser) attempted.add(browser);
          else noBrowserDestroyAttempted = true;
          Promise.resolve().then(() => state.client.destroy()).then(
            () => { destroyCompleted = true; },
            e => log.warn(`Hosted client ${id} destroy failed:`, e.message),
          );
        }
        const children = [...state.children.values()];
        if (state.initSettled && children.every(record => !processAlive(record)) &&
            (children.length > 0 || !browser || destroyCompleted)) {
          state.stopped = true;
          return;
        }
        const elapsed = Date.now() - started;
        for (const record of children) {
          if (!processAlive(record)) continue;
          const signal = elapsed >= times.close + times.term ? 'SIGKILL' : elapsed >= times.close ? 'SIGTERM' : null;
          if (signal && record.lastSignal !== signal) {
            record.lastSignal = signal;
            count(signal === 'SIGKILL' ? 'browserForceKills' : 'browserTerminations');
            try { signalChild(record, signal); } catch (e) {
              log.warn(`Hosted browser ${id} ${signal} failed:`, e.message);
            }
          }
        }
        if (elapsed >= times.close + times.term + times.kill) {
          count('cleanupFailures');
          const failure = error(`Browser cleanup could not be confirmed for session ${id}; refusing replacement.`);
          // A timed-out promise is NOT cancellation. Do not release ownership,
          // delete LocalAuth, or reuse the profile while initialization survives.
          state.lastError = failure.message;
          state.retryPaused = true;
          unsafe(failure);
          throw failure;
        }
        await new Promise(resolve => setTimeout(resolve, times.poll));
      }
    })();
    return state.cleanupPromise;
  }

  async function stopOwned(id, state, { logout = false, clearProfile = false, release = true } = {}) {
    if (state.unsafeCleanup) throw state.unsafeCleanup;
    state.closing = true;
    if (['running', 'connecting', 'initializing', 'qr_ready'].includes(state.status)) state.status = 'disconnected';
    let logoutFailure = null;
    if (logout) {
      try { await bounded(Promise.resolve().then(() => state.client.logout()), times.logout, 'WhatsApp logout'); }
      catch (e) { logoutFailure = e; }
    }
    await stopClient(id, state);
    if (logoutFailure?.message.endsWith(' timed out')) {
      // logout may delete the profile later. Refuse profile reuse until the
      // container exits rather than letting that promise delete a new session.
      state.unsafeCleanup = logoutFailure;
      unsafe(logoutFailure);
      throw logoutFailure;
    }
    if (clearProfile) {
      try {
        await bounded(Promise.resolve().then(() => state.client.authStrategy?.logout()), times.logout, 'LocalAuth cleanup');
      } catch (e) { state.unsafeCleanup = e; unsafe(e); throw e; }
    }
    if (release) await bounded(releaseLock(id), times.redis, 'Release session lease');
  }

  function failSession(id, state, event, reason, { retry = false, logout = false, release = true } = {}) {
    if (!current(id, state)) return Promise.resolve();
    state.closing = true; // Synchronous fencing, before waiting for the queue.
    state.status = event === 'disconnected' || event === 'lease_lost' ? 'disconnected' : 'failed';
    state.lastError = String(reason || event);
    state.qr = null;
    state.qrGeneratedAt = 0;
    if (event === 'disconnected') count('reconnects');
    return run(id, async () => {
      if (sessions.get(id) !== state) return;
      // Keep notification and browser cleanup together inside this generation's
      // operation; a replacement must not race an old status callback either.
      const notification = stopping ? Promise.resolve() : notify(id, state, event,
        event === 'disconnected' ? { reason: state.lastError } : { error: state.lastError });
      const results = await Promise.allSettled([
        stopOwned(id, state, { logout, clearProfile: logout, release: false }), notification,
      ]);
      if (results[0].status === 'rejected') throw results[0].reason;
      if (release) await bounded(releaseLock(id), times.redis, 'Release failed lease');
      if (retry && !stopping) retryFailure(id, state);
    });
  }

  function attachEvents(id, state) {
    const client = state.client;
    const originalOn = client.on;
    // Wrap only the application's registrations, not whatsapp-web.js internals.
    client.on = function on(event, handler) {
      const guarded = (...args) => {
        if (!current(id, state)) return;
        if (event === 'auth_failure') return background(failSession(id, state, event, args[0]));
        if (event === 'disconnected') return background(failSession(id, state, event, args[0], {
          retry: String(args[0] || '').toUpperCase() !== 'LOGOUT',
        }));
        background(events.run({ id, state }, () => Promise.resolve().then(() => {
          if (current(id, state)) return handler(...args);
        })));
      };
      return originalOn.call(this, event, guarded);
    };
    try { wireClientEvents(id, state); } finally { client.on = originalOn; }
  }

  async function createUnlocked(id, phone = '') {
    if (stopping) throw error('Gateway is shutting down.');
    let existing = sessions.get(id);
    if (existing?.status === 'expired' && phone) {
      await stopOwned(id, existing);
      sessions.delete(id);
      existing = null;
    }
    if (existing) {
      if (phone) existing.requestedPhone = phone;
      if (current(id, existing)) await reconcileClientState(id, existing);
      return existing;
    }
    const retry = retries.get(id);
    if (retry && (retry.paused || retry.at > now())) throw error('Session recovery is paused or backing off. Refresh QR to retry.');
    if (activeCount() >= maxSessions) throw error('Gateway session capacity reached; assign this account to another shard.');
    reservations.add(id);
    let locked = false;
    let state;
    try {
      if (!(await acquireLock(id))) { count('leaseConflicts'); throw error('Session is active on another gateway instance.', 409); }
      locked = true;
      if (stopping) throw error('Gateway is shutting down.');
      state = {
        client: createClient(id), status: 'initializing', qr: null, qrGeneratedAt: 0,
        qrIdleStartedAt: 0, phoneNumber: '', requestedPhone: phone, lastError: '',
        historySyncPromise: null, historySynced: false, historyResult: null,
        historyError: '', readyPromise: null, localSendTokens: new Set(),
        localMessageIds: new Map(), restoreRetryAt: 0, retryPaused: false,
        closing: false, stopped: false, initSettled: false, children: new Map(),
      };
      sessions.set(id, state);
      count('sessionsCreated');
      attachEvents(id, state);
      state.browserWatch = setInterval(() => observeBrowser(id, state), times.poll);
      state.browserWatch.unref?.();
      // The observed initialization promise deliberately excludes failSession:
      // cleanup waits for initialization, so chaining cleanup here deadlocks.
      state.initialization = Promise.resolve().then(() => state.client.initialize()).then(
        () => { state.initSettled = true; observeBrowser(id, state); },
        e => {
          state.initSettled = true;
          observeBrowser(id, state);
          background(failSession(id, state, 'failed', e.message || String(e), { retry: true }));
        },
      );
      return state;
    } catch (e) {
      if (state) {
        state.initSettled = true;
        await stopOwned(id, state);
        sessions.delete(id);
      } else if (locked) {
        await bounded(releaseLock(id), times.redis, 'Release unused lease');
      }
      if (locked && !stopping) retryFailure(id);
      throw e;
    } finally { reservations.delete(id); }
  }

  function createSession(id, phone = '') {
    valid(id);
    return run(id, () => {
      if (phone) suppressedRestores.delete(id);
      return createUnlocked(id, phone);
    });
  }
  function refreshQr(id) {
    valid(id);
    return run(id, async () => {
      if (stopping) throw error('Gateway is shutting down.');
      const state = sessions.get(id);
      if (state && current(id, state)) await reconcileClientState(id, state);
      if (state?.status === 'running' && current(id, state)) return state;
      const phone = state?.requestedPhone || '';
      if (state) { await stopOwned(id, state); sessions.delete(id); }
      suppressedRestores.delete(id);
      retries.delete(id); // Only an explicit refresh resets the circuit breaker.
      return createUnlocked(id, phone);
    });
  }
  function logoutSession(id) {
    valid(id);
    return run(id, async () => {
      suppressedRestores.add(id);
      const state = sessions.get(id);
      if (!state) return; // Never delete an unknown/newer owner's lease.
      await stopOwned(id, state, { logout: true, clearProfile: true, release: false });
      await notify(id, state, 'logout', {});
      await bounded(releaseLock(id), times.redis, 'Release logout lease');
      sessions.delete(id);
      retries.delete(id);
    });
  }
  function restoreSessions() {
    if (restorePromise) return restorePromise;
    restorePromise = (async () => {
      if (stopping) return;
      for (const entry of await readProfiles()) {
        if (stopping) break;
        if (!entry.isDirectory() || !entry.name.startsWith('session-')) continue;
        const id = entry.name.slice(8);
        try {
          valid(id);
          await run(id, async () => {
            if (stopping || suppressedRestores.has(id)) return;
            const retry = retries.get(id);
            const state = sessions.get(id);
            if (state) {
              if (!state.stopped || !retry || retry.paused || retry.at > now()) return;
              sessions.delete(id);
            }
            await createUnlocked(id);
          });
        } catch (e) { log.warn(`Could not restore session ${id}:`, e.message); }
      }
    })().finally(() => { restorePromise = null; });
    return restorePromise;
  }
  async function expireIdleQrSessions(idleMs) {
    if (stopping) return;
    await Promise.allSettled([...sessions.keys()].map(id => run(id, async () => {
      const state = sessions.get(id);
      if (!state || !current(id, state) || state.status !== 'qr_ready' || !state.qrIdleStartedAt) return;
      if (now() - state.qrIdleStartedAt < idleMs) return;
      state.status = 'expired';
      state.lastError = 'QR session expired. Refresh QR to reconnect.';
      await stopOwned(id, state, { clearProfile: true, release: false });
      state.qrIdleStartedAt = 0;
      await notify(id, state, 'disconnected', { reason: state.lastError });
      await bounded(releaseLock(id), times.redis, 'Release expired lease');
    })));
  }
  function shutdown() {
    if (shutdownPromise) return shutdownPromise;
    stopping = true;
    shutdownPromise = Promise.allSettled([...new Set([...sessions.keys(), ...operations.keys()])].map(id => run(id, async () => {
      const state = sessions.get(id);
      if (!state) return;
      await stopOwned(id, state);
      sessions.delete(id);
    }))).then(results => {
      const failed = results.find(result => result.status === 'rejected');
      if (failed) throw failed.reason;
    });
    return shutdownPromise;
  }

  return {
    createSession, refreshQr, logoutSession, restoreSessions, expireIdleQrSessions,
    shutdown, failSession, current, callbackAllowed, stopClient,
    fenceSession: (id, state, reason) => failSession(id, state, 'lease_lost', reason, { release: false, retry: true }),
    markRunning: (id, state) => { if (current(id, state) && state.runningSince == null) state.runningSince = now(); },
    diagnostics: () => ({ shuttingDown: stopping, active: activeCount(), pending: reservations.size,
      retryPaused: [...retries.values()].filter(item => item.paused).length }),
  };
}

module.exports = { createHostedLifecycle, hasExited, signalOwnedChild, ownedProcessAlive, bounded };
