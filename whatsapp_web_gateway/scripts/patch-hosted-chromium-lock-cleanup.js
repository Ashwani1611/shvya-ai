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
      console.log(`Hosted Chromium lock cleanup patch already applied: ${label}`);
      return;
    }
    throw new Error(`Unable to apply Hosted Chromium lock cleanup patch: ${label}`);
  }
  if (source.indexOf(before, first + before.length) !== -1) {
    throw new Error(`Hosted Chromium lock cleanup signature is not unique: ${label}`);
  }
  source = source.replace(before, after);
  console.log(`Applied Hosted Chromium lock cleanup patch: ${label}`);
}

if (!/require\(['"]path['"]\)/.test(source)) {
  replaceOnce(
    "const fs = require('fs');",
    lines("const fs = require('fs');", "const path = require('path');"),
    'require path module for lock cleanup',
  );
}

replaceOnce(
  "async function createSession(sessionId, requestedPhone = '') {",
  lines(
    'const sessionFailureTrackers = new Map(); // sessionId -> { attempts }',
    'const BASE_RETRY_MS = 30000;              // matches the 30s restoreSessions poll floor',
    'const MAX_RETRY_MS = 30 * 60 * 1000;      // never wait longer than 30min between attempts',
    'const MAX_RESTORE_ATTEMPTS = 6;           // after this many, log loudly but keep retrying at MAX_RETRY_MS',
    '',
    'function getFailureTracker(sessionId) {',
    '  let tracker = sessionFailureTrackers.get(sessionId);',
    '  if (!tracker) {',
    '    tracker = { attempts: 0 };',
    '    sessionFailureTrackers.set(sessionId, tracker);',
    '  }',
    '  return tracker;',
    '}',
    '',
    'function clearFailureTracker(sessionId) {',
    '  sessionFailureTrackers.delete(sessionId);',
    '}',
    '',
    'async function cleanSessionChromiumLocks(sessionId) {',
    "  const profilePath = path.join(AUTH_PATH, 'session-' + sessionId);",
    "  const lockNames = ['SingletonCookie', 'SingletonLock', 'SingletonSocket'];",
    '  for (const lockName of lockNames) {',
    '    const lockPath = path.join(profilePath, lockName);',
    '    try {',
    '      await fs.promises.unlink(lockPath);',
    "      console.warn('Removed stale ' + lockName + ' for session ' + sessionId);",
    '    } catch (error) {',
    "      if (error && error.code !== 'ENOENT') {",
    "        console.warn('Could not remove ' + lockPath + ':', error.message);",
    '      }',
    '    }',
    '  }',
    '}',
    '',
    "async function createSession(sessionId, requestedPhone = '') {",
  ),
  'add Chromium stale-lock cleanup helpers and failure tracker',
);

replaceOnce(
  lines(
    "    // Failed Chromium startup must never keep this process's lease alive.",
    '    // Mark only initialization failures for automatic persisted-profile retry.',
    '    state.restoreRetryAt = Date.now() + 30000;',
    '    await releaseLock(sessionId).catch(() => {});',
  ),
  lines(
    "    // Failed Chromium startup must never keep this process's lease alive.",
    "    // Back off exponentially (capped) so a stuck profile can't spawn Chromium every 30s forever.",
    '    const tracker = getFailureTracker(sessionId);',
    '    tracker.attempts += 1;',
    '    const backoffMs = Math.min(BASE_RETRY_MS * 2 ** (tracker.attempts - 1), MAX_RETRY_MS);',
    '    state.restoreRetryAt = Date.now() + backoffMs;',
    '    if (tracker.attempts >= MAX_RESTORE_ATTEMPTS) {',
    '      console.error(',
    '        `Hosted session ${sessionId} failed ${tracker.attempts} times in a row `',
    '        + `(last error: ${state.lastError}); backing off to ${Math.round(MAX_RETRY_MS / 60000)}min `',
    "        + 'intervals. Needs manual investigation.',",
    '      );',
    '    }',
    '    await releaseLock(sessionId).catch(() => {});',
  ),
  'exponential backoff for repeated initialization failures',
);

replaceOnce(
  lines(
    "      console.warn('Retrying failed persisted Hosted session ' + sessionId);",
    '      sessions.delete(sessionId);',
    '      await releaseLock(sessionId).catch(() => {});',
    "      await destroyClientBounded(sessionId, existing, 'retry failed restore');",
    '    }',
  ),
  lines(
    "      console.warn('Retrying failed persisted Hosted session ' + sessionId);",
    '      sessions.delete(sessionId);',
    '      await releaseLock(sessionId).catch(() => {});',
    "      await destroyClientBounded(sessionId, existing, 'retry failed restore');",
    '      // Clean any stale SingletonLock/Cookie/Socket left by the failed Chromium',
    '      // launch before trying again — otherwise every retry fails the same way forever.',
    '      await cleanSessionChromiumLocks(sessionId).catch((error) => {',
    "        console.warn('Could not clean Chromium locks for ' + sessionId + ':', error.message);",
    '      });',
    '    }',
  ),
  'clean stale Chromium locks before retrying a failed session',
);

fs.writeFileSync(target, source);

for (const marker of [
  'function cleanSessionChromiumLocks',
  'function getFailureTracker',
  'MAX_RESTORE_ATTEMPTS',
  'Could not clean Chromium locks for ',
]) {
  if (!source.includes(marker)) {
    throw new Error(`Hosted Chromium lock cleanup verification failed: ${marker}`);
  }
}

console.log('Hosted Chromium lock cleanup runtime patch complete.');
