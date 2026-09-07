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
      console.log(`Hosted failed-session retry patch already applied: ${label}`);
      return;
    }
    throw new Error(`Unable to apply Hosted failed-session retry patch: ${label}`);
  }
  if (source.indexOf(before, first + before.length) !== -1) {
    throw new Error(`Hosted failed-session retry signature is not unique: ${label}`);
  }
  source = source.replace(before, after);
  console.log(`Applied Hosted failed-session retry patch: ${label}`);
}

replaceOnce(
  lines(
    '    readyPromise: null,',
    '    localSendTokens: new Set(),',
    '    localMessageIds: new Map(),',
  ),
  lines(
    '    readyPromise: null,',
    '    localSendTokens: new Set(),',
    '    localMessageIds: new Map(),',
    '    restoreRetryAt: 0,',
  ),
  'track retry time for initialization failures',
);

replaceOnce(
  lines(
    "    // Failed Chromium startup must never keep this process's lease alive.",
    '    await releaseLock(sessionId).catch(() => {});',
  ),
  lines(
    "    // Failed Chromium startup must never keep this process's lease alive.",
    '    // Mark only initialization failures for automatic persisted-profile retry.',
    '    state.restoreRetryAt = Date.now() + 30000;',
    '    await releaseLock(sessionId).catch(() => {});',
  ),
  'schedule failed browser initialization for retry',
);

replaceOnce(
  lines(
    "    const sessionId = entry.name.slice('session-'.length);",
    '    if (!sessionId) continue;',
    '    try {',
    '      await createSession(sessionId);',
  ),
  lines(
    "    const sessionId = entry.name.slice('session-'.length);",
    '    if (!sessionId) continue;',
    '    const existing = sessions.get(sessionId);',
    '    if (',
    '      existing &&',
    "      existing.status === 'failed' &&",
    '      existing.restoreRetryAt &&',
    '      existing.restoreRetryAt <= Date.now()',
    '    ) {',
    "      console.warn('Retrying failed persisted Hosted session ' + sessionId);",
    '      sessions.delete(sessionId);',
    '      await releaseLock(sessionId).catch(() => {});',
    "      await destroyClientBounded(sessionId, existing, 'retry failed restore');",
    '    }',
    '    try {',
    '      await createSession(sessionId);',
  ),
  'recreate failed persisted sessions after retry delay',
);

fs.writeFileSync(target, source);

for (const marker of [
  'restoreRetryAt: 0',
  'state.restoreRetryAt = Date.now() + 30000',
  'Retrying failed persisted Hosted session ',
  "retry failed restore",
]) {
  if (!source.includes(marker)) {
    throw new Error(`Hosted failed-session retry verification failed: ${marker}`);
  }
}

console.log('Hosted failed-session retry runtime patch complete.');
