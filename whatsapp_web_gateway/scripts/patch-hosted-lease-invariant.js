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
      console.log(`Hosted lease invariant patch already applied: ${label}`);
      return;
    }
    throw new Error(`Unable to apply Hosted lease invariant patch: ${label}`);
  }
  if (source.indexOf(before, first + before.length) !== -1) {
    throw new Error(`Hosted lease invariant signature is not unique: ${label}`);
  }
  source = source.replace(before, after);
  console.log(`Applied Hosted lease invariant patch: ${label}`);
}

replaceOnce(
  lines(
    'async function renewLocks() {',
    '  if (!redis || shuttingDown) return;',
    '  for (const sessionId of sessions.keys()) {',
    '    const key = lockKey(sessionId);',
    '    ' + 'const current = await redis.get(key);',
    '    if (current === INSTANCE_ID) await redis.expire(key, 90);',
    '  }',
    '}',
  ),
  lines(
    'async function renewLocks() {',
    '  if (!redis || shuttingDown) return;',
    '  for (const [sessionId, state] of Array.from(sessions.entries())) {',
    "    // Initialization failures intentionally release their lease and are",
    "    // recreated by the persisted-session retry loop. Do not reacquire a",
    "    // lease for a failed client that should be torn down/retried.",
    "    if (!state || state.status === 'failed') continue;",
    '    try {',
    '      // acquireLock() renews our lease when present and safely recreates it',
    '      // with SET NX when it disappeared. A foreign owner is never stolen.',
    '      const owned = await acquireLock(sessionId);',
    '      if (owned) continue;',
    '',
    '      // An in-memory browser without Redis ownership is unsafe: another',
    '      // gateway may now own the same LocalAuth profile. Tear this client',
    '      // down and let restoreSessions() retry after the foreign lease ends.',
    '      if (sessions.get(sessionId) !== state) continue;',
    "      console.error('Hosted session ' + sessionId + ' lost Redis lease; tearing down local client to prevent dual ownership.');",
    '      sessions.delete(sessionId);',
    "      await destroyClientBounded(sessionId, state, 'lost Redis lease');",
    '    } catch (error) {',
    "      // A transient Redis error is not proof that ownership was lost. Keep",
    "      // the local client and retry on the next renewal tick.",
    "      console.warn('Could not renew Hosted session lease ' + sessionId + ':', error.message);",
    '    }',
    '  }',
    '}',
  ),
  'reacquire missing active-session leases and fence foreign ownership',
);

fs.writeFileSync(target, source);

for (const marker of [
  'const owned = await acquireLock(sessionId)',
  'lost Redis lease; tearing down local client to prevent dual ownership.',
  "state.status === 'failed'",
]) {
  if (!source.includes(marker)) {
    throw new Error(`Hosted lease invariant verification failed: ${marker}`);
  }
}

console.log('Hosted lease invariant runtime patch complete.');
