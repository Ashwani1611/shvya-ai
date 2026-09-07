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
      console.log(`Hosted QR expiry patch already applied: ${label}`);
      return;
    }
    throw new Error(`Unable to apply Hosted QR expiry patch: ${label}`);
  }
  if (source.indexOf(before, first + before.length) !== -1) {
    throw new Error(`Hosted QR expiry signature is not unique: ${label}`);
  }
  source = source.replace(before, after);
  console.log(`Applied Hosted QR expiry patch: ${label}`);
}

replaceOnce(
  'const QR_EXPIRES_SECONDS = 60;',
  lines(
    'const QR_EXPIRES_SECONDS = 60;',
    'const QR_IDLE_TIMEOUT_MS = Math.max(',
    '  120000,',
    "  Number(process.env.WHATSAPP_WEB_QR_IDLE_TIMEOUT_MS || 10 * 60 * 1000),",
    ');',
  ),
  'configure QR idle timeout',
);

replaceOnce(
  lines(
    "  if (!state || state.status === 'running' || state.status === 'failed' || state.status === 'disconnected') {",
    '    return;',
    '  }',
  ),
  lines(
    "  if (!state || state.status === 'running' || state.status === 'failed' || state.status === 'disconnected' || state.status === 'expired') {",
    '    return;',
    '  }',
  ),
  'skip state probes for expired sessions',
);

replaceOnce(
  "    if (!state || state.status === 'failed') continue;",
  "    if (!state || state.status === 'failed' || state.status === 'expired') continue;",
  'do not reacquire leases for expired QR sessions',
);

replaceOnce(
  lines(
    "  client.on('qr', async (rawQr) => {",
    "    state.status = 'qr_ready';",
    '    state.qr = await QRCode.toDataURL(rawQr, { width: 304, margin: 1 });',
    '    state.qrGeneratedAt = Date.now();',
    "    state.lastError = '';",
    "    await callback(sessionId, 'qr');",
    '  });',
  ),
  lines(
    "  client.on('qr', async (rawQr) => {",
    "    state.status = 'qr_ready';",
    '    state.qr = await QRCode.toDataURL(rawQr, { width: 304, margin: 1 });',
    '    state.qrGeneratedAt = Date.now();',
    '    if (!state.qrIdleStartedAt) state.qrIdleStartedAt = Date.now();',
    "    state.lastError = '';",
    "    await callback(sessionId, 'qr');",
    '  });',
  ),
  'track absolute QR idle window',
);

replaceOnce(
  lines(
    "  client.on('authenticated', async () => {",
    "    state.status = 'connecting';",
    '    state.qr = null;',
    '    state.qrGeneratedAt = 0;',
  ),
  lines(
    "  client.on('authenticated', async () => {",
    "    state.status = 'connecting';",
    '    state.qr = null;',
    '    state.qrGeneratedAt = 0;',
    '    state.qrIdleStartedAt = 0;',
  ),
  'clear QR idle timer after authentication',
);

replaceOnce(
  lines(
    '    qr: null,',
    '    qrGeneratedAt: 0,',
    "    phoneNumber: '',",
  ),
  lines(
    '    qr: null,',
    '    qrGeneratedAt: 0,',
    '    qrIdleStartedAt: 0,',
    "    phoneNumber: '',",
  ),
  'store QR idle start time',
);

replaceOnce(
  lines(
    '  const existing = sessions.get(sessionId);',
    '  if (existing) {',
    '    if (requestedPhone) existing.requestedPhone = requestedPhone;',
    '    await reconcileClientState(sessionId, existing);',
    '    return existing;',
    '  }',
  ),
  lines(
    '  let existing = sessions.get(sessionId);',
    "  if (existing && existing.status === 'expired' && requestedPhone) {",
    '    sessions.delete(sessionId);',
    '    await releaseLock(sessionId).catch(() => {});',
    "    await destroyClientBounded(sessionId, existing, 'restart expired QR session');",
    '    existing = null;',
    '  }',
    '  if (existing) {',
    '    if (requestedPhone) existing.requestedPhone = requestedPhone;',
    '    await reconcileClientState(sessionId, existing);',
    '    return existing;',
    '  }',
  ),
  'allow explicit session creation to restart an expired QR session',
);

replaceOnce(
  lines(
    'async function restoreSessions() {',
    '  if (shuttingDown) return;',
  ),
  lines(
    'async function expireIdleQrSessions() {',
    '  if (shuttingDown) return;',
    '  const now = Date.now();',
    '  for (const [sessionId, state] of Array.from(sessions.entries())) {',
    "    if (!state || state.status !== 'qr_ready' || !state.qrIdleStartedAt) continue;",
    '    if (now - state.qrIdleStartedAt < QR_IDLE_TIMEOUT_MS) continue;',
    "    state.status = 'expired';",
    '    state.qr = null;',
    '    state.qrGeneratedAt = 0;',
    '    state.qrIdleStartedAt = 0;',
    "    state.lastError = 'QR session expired after waiting too long for a scan. Refresh QR to reconnect.';",
    '    await releaseLock(sessionId).catch(() => {});',
    '    try {',
    '      const authStrategy = state.client && state.client.authStrategy;',
    "      if (authStrategy && typeof authStrategy.logout === 'function') {",
    '        await withTimeout(',
    '          Promise.resolve().then(() => authStrategy.logout()),',
    '          5000,',
    "          'expire QR LocalAuth ' + sessionId,",
    '        );',
    '      }',
    '    } catch (error) {',
    "      console.warn('Could not clean expired QR LocalAuth ' + sessionId + ':', error.message);",
    '    }',
    "    await destroyClientBounded(sessionId, state, 'expire idle QR');",
    "    state.status = 'expired';",
    '    state.qr = null;',
    '    state.qrGeneratedAt = 0;',
    '    state.qrIdleStartedAt = 0;',
    "    console.warn('Expired idle Hosted QR session ' + sessionId);",
    "    await callback(sessionId, 'disconnected', { reason: state.lastError });",
    '  }',
    '}',
    '',
    'async function restoreSessions() {',
    '  if (shuttingDown) return;',
  ),
  'expire abandoned QR sessions without deleting active accounts',
);

replaceOnce(
  '  setInterval(() => renewLocks().catch(() => {}), 30000).unref();',
  lines(
    '  setInterval(() => renewLocks().catch(() => {}), 30000).unref();',
    '  setInterval(() => {',
    '    expireIdleQrSessions().catch((error) => {',
    "      console.warn('Could not expire idle Hosted QR sessions:', error.message);",
    '    });',
    '  }, 30000).unref();',
  ),
  'schedule QR idle cleanup',
);

replaceOnce(
  "app.get('/health', (_req, res) => res.json({ ok: true, sessions: sessions.size }));",
  lines(
    "app.get('/health', (_req, res) => {",
    '  const statuses = {};',
    '  for (const state of sessions.values()) {',
    "    const status = String((state && state.status) || 'unknown');",
    '    statuses[status] = (statuses[status] || 0) + 1;',
    '  }',
    '  res.json({ ok: true, sessions: sessions.size, statuses });',
    '});',
  ),
  'expose session status counts in health endpoint',
);

fs.writeFileSync(target, source);

for (const marker of [
  'QR_IDLE_TIMEOUT_MS',
  'qrIdleStartedAt: 0',
  'async function expireIdleQrSessions()',
  'Expired idle Hosted QR session ',
  "state.status === 'expired'",
  'statuses[status] = (statuses[status] || 0) + 1',
]) {
  if (!source.includes(marker)) {
    throw new Error(`Hosted QR expiry verification failed: ${marker}`);
  }
}

console.log('Hosted QR expiry runtime patch complete.');
