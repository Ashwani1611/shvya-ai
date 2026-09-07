'use strict';

const fs = require('fs');
const path = require('path');

const target = path.join(process.cwd(), 'src', 'index.js');
let source = fs.readFileSync(target, 'utf8');

function lines(...items) {
  return items.join('\n');
}

function replaceOnce(before, after, label) {
  const first = source.indexOf(before);
  if (first === -1) {
    if (source.includes(after)) {
      console.log(`Hosted session recovery patch already applied: ${label}`);
      return;
    }
    throw new Error(`Unable to apply Hosted session recovery patch: ${label}`);
  }
  if (source.indexOf(before, first + before.length) !== -1) {
    throw new Error(`Hosted session recovery signature is not unique: ${label}`);
  }
  source = source.replace(before, after);
  console.log(`Applied Hosted session recovery patch: ${label}`);
}

replaceOnce(
  lines('const sessions = new Map();', 'let redis = null;'),
  lines('const sessions = new Map();', 'let redis = null;', 'let shuttingDown = false;'),
  'track gateway shutdown state',
);

replaceOnce(
  lines(
    'async function acquireLock(sessionId) {',
    '  if (!redis) return true;',
    '  const key = lockKey(sessionId);',
    '  const current = await redis.get(key);',
    '  if (current === INSTANCE_ID) {',
    '    await redis.expire(key, 90);',
    '    return true;',
    '  }',
    "  const result = await redis.set(key, INSTANCE_ID, { NX: true, EX: 90 });",
    "  return result === 'OK';",
    '}',
  ),
  lines(
    'async function acquireLock(sessionId) {',
    '  if (!redis) return true;',
    '  const key = lockKey(sessionId);',
    '  const current = await redis.get(key);',
    '  if (current === INSTANCE_ID) {',
    '    await redis.expire(key, 90);',
    '    return true;',
    '  }',
    "  const result = await redis.set(key, INSTANCE_ID, { NX: true, EX: 90 });",
    "  if (result === 'OK') return true;",
    '  const [owner, ttl] = await Promise.all([redis.get(key), redis.ttl(key)]);',
    "  console.warn('Hosted session ' + sessionId + ' lease conflict: local=' + INSTANCE_ID + ' owner=' + (owner || 'unknown') + ' ttl=' + ttl + 's');",
    '  return false;',
    '}',
  ),
  'diagnose foreign Redis lease conflicts without stealing them',
);

replaceOnce(
  lines('async function renewLocks() {', '  if (!redis) return;'),
  lines('async function renewLocks() {', '  if (!redis || shuttingDown) return;'),
  'stop renewing leases during shutdown',
);

replaceOnce(
  lines(
    'async function releaseLock(sessionId) {',
    '  if (!redis) return;',
    '  const key = lockKey(sessionId);',
    '  const current = await redis.get(key);',
    '  if (current === INSTANCE_ID) await redis.del(key);',
    '}',
  ),
  lines(
    'async function releaseLock(sessionId) {',
    '  if (!redis) return;',
    '  const key = lockKey(sessionId);',
    '  const current = await redis.get(key);',
    '  if (current === INSTANCE_ID) await redis.del(key);',
    '}',
    '',
    "async function destroyClientBounded(sessionId, state, reason = 'destroy') {",
    '  if (!state || !state.client) return;',
    '  try {',
    '    await withTimeout(',
    '      Promise.resolve().then(() => state.client.destroy()),',
    '      8000,',
    "      reason + ' ' + sessionId,",
    '    );',
    '  } catch (error) {',
    "    console.warn('Could not ' + reason + ' Hosted client ' + sessionId + ':', error.message);",
    '  }',
    '}',
  ),
  'bound Puppeteer client teardown',
);

replaceOnce(
  lines(
    "async function createSession(sessionId, requestedPhone = '') {",
    "  if (!/^[-_\\w]+$/i.test(sessionId)) throw new Error('Invalid session id.');",
  ),
  lines(
    "async function createSession(sessionId, requestedPhone = '') {",
    "  if (!/^[-_\\w]+$/i.test(sessionId)) throw new Error('Invalid session id.');",
    '  if (shuttingDown) {',
    "    const error = new Error('Gateway is shutting down.');",
    '    error.statusCode = 503;',
    '    throw error;',
    '  }',
  ),
  'reject new sessions while gateway is shutting down',
);

replaceOnce(
  lines(
    '  client.initialize().catch(async (error) => {',
    "    state.status = 'failed';",
    '    state.qr = null;',
    '    state.qrGeneratedAt = 0;',
    '    state.lastError = error.message || String(error);',
    "    await callback(sessionId, 'failed', { error: state.lastError });",
    '  });',
  ),
  lines(
    '  client.initialize().catch(async (error) => {',
    "    state.status = 'failed';",
    '    state.qr = null;',
    '    state.qrGeneratedAt = 0;',
    '    state.lastError = error.message || String(error);',
    "    console.warn('Hosted session ' + sessionId + ' failed to initialize:', state.lastError);",
    "    await callback(sessionId, 'failed', { error: state.lastError });",
    "    // Failed Chromium startup must never keep this process's lease alive.",
    '    await releaseLock(sessionId).catch(() => {});',
    '  });',
  ),
  'release lease after browser initialization failure',
);

replaceOnce(
  lines(
    '  const requestedPhone = current.requestedPhone;',
    '  try { await current.client.destroy(); } catch (_) {}',
    '  sessions.delete(sessionId);',
    '  await releaseLock(sessionId).catch(() => {});',
    '  return createSession(sessionId, requestedPhone);',
  ),
  lines(
    '  const requestedPhone = current.requestedPhone;',
    '  // Stop renewal and release our lease before browser teardown.',
    '  sessions.delete(sessionId);',
    '  await releaseLock(sessionId).catch(() => {});',
    "  await destroyClientBounded(sessionId, current, 'refresh');",
    '  return createSession(sessionId, requestedPhone);',
  ),
  'release refresh lease before Chromium teardown',
);

replaceOnce(
  lines(
    '  try { await state.client.logout(); } catch (_) {}',
    '  try { await state.client.destroy(); } catch (_) {}',
    '  sessions.delete(sessionId);',
    '  await releaseLock(sessionId);',
    "  await callback(sessionId, 'logout');",
  ),
  lines(
    '  sessions.delete(sessionId);',
    '  await releaseLock(sessionId).catch(() => {});',
    '  try {',
    '    await withTimeout(',
    '      Promise.resolve().then(() => state.client.logout()),',
    '      5000,',
    "      'logout ' + sessionId,",
    '    );',
    '  } catch (error) {',
    "    console.warn('Could not logout Hosted client ' + sessionId + ':', error.message);",
    '  }',
    "  await destroyClientBounded(sessionId, state, 'destroy after logout');",
    "  await callback(sessionId, 'logout');",
  ),
  'release logout lease before Chromium teardown',
);

replaceOnce(
  lines(
    'async function restoreSessions() {',
    '  const entries = await fs.promises.readdir(AUTH_PATH, { withFileTypes: true });',
  ),
  lines(
    'async function restoreSessions() {',
    '  if (shuttingDown) return;',
    '  const entries = await fs.promises.readdir(AUTH_PATH, { withFileTypes: true });',
  ),
  'skip restore work during shutdown',
);

replaceOnce(
  lines(
    'async function shutdown() {',
    '  for (const [sessionId, state] of sessions.entries()) {',
    '    try { await state.client.destroy(); } catch (_) {}',
    '    await releaseLock(sessionId).catch(() => {});',
    '  }',
    '  if (redis) await redis.quit().catch(() => {});',
    '  process.exit(0);',
    '}',
  ),
  lines(
    'async function shutdown() {',
    '  if (shuttingDown) return;',
    '  shuttingDown = true;',
    '  const activeSessions = Array.from(sessions.entries());',
    '  // Release every owned Redis lease first and stop renewal before touching',
    '  // Puppeteer. A slow Chromium teardown can no longer strand ownership.',
    '  sessions.clear();',
    '  await Promise.allSettled(',
    '    activeSessions.map(([sessionId]) => releaseLock(sessionId)),',
    '  );',
    '  await Promise.allSettled(',
    "    activeSessions.map(([sessionId, state]) => destroyClientBounded(sessionId, state, 'shutdown')),",
    '  );',
    '  if (redis) {',
    '    try {',
    "      await withTimeout(redis.quit(), 3000, 'Redis shutdown');",
    '    } catch (error) {',
    "      console.warn('Could not close Redis cleanly:', error.message);",
    '      try { redis.disconnect(); } catch (_) {}',
    '    }',
    '  }',
    '  process.exit(0);',
    '}',
  ),
  'release all leases before bounded shutdown teardown',
);

replaceOnce(
  lines(
    '    await startRedis();',
    '    await restoreSessions();',
    "    app.listen(PORT, '0.0.0.0', () => {",
  ),
  lines(
    '    await startRedis();',
    '    await restoreSessions();',
    '    // If an old process was SIGKILLed, its foreign lease expires naturally.',
    '    // Re-scan profiles until they can be acquired; never DEL/steal that lock.',
    '    setInterval(() => {',
    '      if (shuttingDown) return;',
    '      restoreSessions().catch((error) => {',
    "        console.warn('Could not retry persisted Hosted sessions:', error.message);",
    '      });',
    '    }, 30000).unref();',
    "    app.listen(PORT, '0.0.0.0', () => {",
  ),
  'retry persisted sessions after stale lease expiry',
);

fs.writeFileSync(target, source);

for (const marker of [
  'let shuttingDown = false',
  'lease conflict: local=',
  'destroyClientBounded',
  'sessions.clear()',
  'Promise.allSettled',
  'releaseLock(sessionId).catch',
  'Could not retry persisted Hosted sessions:',
]) {
  if (!source.includes(marker)) {
    throw new Error(`Hosted session recovery patch verification failed: ${marker}`);
  }
}

console.log('Hosted session recovery runtime patch complete.');
