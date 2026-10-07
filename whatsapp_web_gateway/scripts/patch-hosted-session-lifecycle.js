'use strict';

const fs = require('fs');
const path = require('path');
const marker = '// SHVYA_HOSTED_SESSION_LIFECYCLE_V1';

// Applied after the other source patches: test the exact Docker runtime,
// including QR expiry, durable callbacks and provider acknowledgement checks.
function patchSource(input) {
  if (input.includes(marker)) return input;
  let source = input;
  function replace(before, after, expected = 1) {
    const count = source.split(before).length - 1;
    if (count !== expected) throw new Error(`Hosted lifecycle patch expected ${expected} matches, found ${count}: ${before.slice(0, 90)}`);
    source = source.split(before).join(after);
  }
  function replaceFunction(name, replacement) {
    const start = source.indexOf(`async function ${name}(`);
    if (start < 0) throw new Error(`Missing Hosted lifecycle function ${name}`);
    const tail = source.slice(start);
    const end = tail.slice(1).search(/\n(?:async )?function /) + 1;
    if (end <= 0) throw new Error(`Missing Hosted lifecycle boundary for ${name}`);
    source = source.slice(0, start) + replacement + '\n' + tail.slice(end);
  }

  replace("const { idempotentSend: rawIdempotentSend } = require('./send-idempotency');", `const { idempotentSend: rawIdempotentSend } = require('./send-idempotency');
const { createSessionOperationQueue } = require('./session-lifecycle');
${marker}`);
  replace('const sessions = new Map();', 'const sessions = new Map();\nconst withSessionOperation = createSessionOperationQueue();');
  replace('function publicSession(sessionId, state) {', `function activeSessionCount() {
  return Array.from(sessions.values()).filter(state => state.status !== 'expired').length;
}

function publicSession(sessionId, state) {`);
  replace('if (sessions.size >= MAX_HOSTED_SESSIONS)', 'if (activeSessionCount() >= MAX_HOSTED_SESSIONS)');
  replace('capacityRemaining: Math.max(0, MAX_HOSTED_SESSIONS - sessions.size)', 'capacityRemaining: Math.max(0, MAX_HOSTED_SESSIONS - activeSessionCount())');
  replace("    status: 'initializing',", "    status: 'initializing',\n    startupStartedAt: Date.now(),");

  replace("async function createSession(sessionId, requestedPhone = '') {", `async function createSession(sessionId, requestedPhone = '') {
  return withSessionOperation(sessionId, () => createSessionUnlocked(sessionId, requestedPhone));
}

async function createSessionUnlocked(sessionId, requestedPhone = '') {`);
  replace("async function refreshQr(sessionId, requestedPhone = '') {", `async function refreshQr(sessionId, requestedPhone = '') {
  return withSessionOperation(sessionId, () => refreshQrUnlocked(sessionId, requestedPhone));
}

async function refreshQrUnlocked(sessionId, requestedPhone = '') {`);
  replace('async function logoutSession(sessionId) {', `async function logoutSession(sessionId) {
  return withSessionOperation(sessionId, () => logoutSessionUnlocked(sessionId));
}

async function logoutSessionUnlocked(sessionId) {`);
  replace('async function fenceSession(sessionId, state, reason) {', `async function fenceSession(sessionId, state, reason) {
  return withSessionOperation(sessionId, () => fenceSessionUnlocked(sessionId, state, reason));
}

async function fenceSessionUnlocked(sessionId, state, reason) {`);
  replace('if (!current) return createSession(sessionId, requestedPhone);', 'if (!current) return createSessionUnlocked(sessionId, requestedPhone);');
  replace('  return createSession(sessionId, requestedPhone);', '  return createSessionUnlocked(sessionId, requestedPhone);');
  replace('  const state = {\n    client: null,', `  if (shuttingDown) {
    await releaseLock(sessionId).catch(() => {});
    const error = new Error('Gateway is shutting down.');
    error.statusCode = 503;
    throw error;
  }

  const state = {
    client: null,`);
  replace(`  // Stop lease renewal before browser teardown so a slow Chromium destroy
  // cannot strand this gateway as the apparent owner of the session.`, `  // Stop renewal, then finish bounded browser teardown before releasing its
  // lease so another instance cannot open the same profile during teardown.`);
  replace(`  await releaseLock(sessionId).catch(() => {});
  await destroyClientBounded(sessionId, current, 'refresh');`, `  await destroyClientBounded(sessionId, current, 'refresh');
  await releaseLock(sessionId).catch(() => {});`);
  replace(`    await releaseLock(sessionId).catch(() => {});
    await destroyClientBounded(sessionId, existing, 'restart expired QR session');`, `    await destroyClientBounded(sessionId, existing, 'restart expired QR session');
    await releaseLock(sessionId).catch(() => {});`);
  replaceFunction('logoutSessionUnlocked', `async function logoutSessionUnlocked(sessionId) {
  sessionProfilePath(sessionId);
  // An orphan profile may still belong to another gateway. Never delete it
  // until this process owns the shared lease too.
  if (!(await acquireLock(sessionId))) {
    const error = new Error('Session is active on another gateway instance.');
    error.statusCode = 409;
    throw error;
  }
  const state = sessions.get(sessionId);
  sessionFailureTrackers.delete(sessionId);
  if (state) {
    state.retired = true;
    state.status = 'disconnecting';
    state.qrVersion = Number(state.qrVersion || 0) + 1;
    // Keep this entry until deletion completes so renewLocks keeps our lease
    // alive even when provider logout takes longer than one lease interval.
  }
  try {
    if (state) {
      // LocalAuth.logout deletes the profile. A timeout does not cancel it;
      // await completion so it cannot delete a newly connected successor.
      try { await state.client.logout(); } catch (_) {}
      await destroyClientBounded(sessionId, state, 'destroy after logout');
    }
    await removeSessionProfile(sessionId);
  } finally {
    if (sessions.get(sessionId) === state) sessions.delete(sessionId);
    await releaseLock(sessionId).catch(() => {});
  }
  if (state) await callback(sessionId, 'logout');
}`);

  replace('  if (!state || !state.client) return;\n  try {', `  if (!state || !state.client) return;
  state.retired = true;
  state.qrVersion = Number(state.qrVersion || 0) + 1;
  if (state.historyRetryTimer) clearTimeout(state.historyRetryTimer);
  try {`);
  replace("if (shuttingDown || sessions.get(sessionId) !== state || state.status === 'expired') return;",
    "if (shuttingDown || sessions.get(sessionId) !== state || state.retired || state.status === 'expired') return;", 9);

  replace("  client.initialize().catch(async (error) => {\n    state.status = 'failed';", `  client.initialize().catch(error => withSessionOperation(sessionId, async () => {
    // A replaced browser often rejects initialize() during teardown. It must
    // never mark the new session failed or release that new browser's lease.
    if (shuttingDown || sessions.get(sessionId) !== state || state.retired) return;
    state.status = 'failed';`);
  replace('    await releaseLock(sessionId).catch(() => {});\n  });\n\n  return state;', '    await releaseLock(sessionId).catch(() => {});\n  }));\n\n  return state;');
  replace("  if (existing && existing.status === 'expired' && requestedPhone) {",
    "  if (existing && ['expired', 'failed', 'disconnected'].includes(existing.status) && requestedPhone) {");
  replace('    if (requestedPhone) existing.requestedPhone = requestedPhone;', `    if (requestedPhone) {
      const changed = digits(requestedPhone) !== digits(existing.requestedPhone);
      existing.requestedPhone = requestedPhone;
      if (changed) {
        // Do not let a recent status poll or an in-flight old ready callback
        // suppress validation when the configured sender identity changes.
        if (existing.readyPromise) await existing.readyPromise;
        existing.lastProbeAt = 0;
      }
    }`);

  replace(`    state.status = 'qr_ready';
    state.qr = await QRCode.toDataURL(rawQr, { width: 304, margin: 1 });
    state.qrGeneratedAt = Date.now();
    if (!state.qrIdleStartedAt) state.qrIdleStartedAt = Date.now();
    state.lastError = '';
    await callback(sessionId, 'qr');`, `    const version = state.qrVersion = Number(state.qrVersion || 0) + 1;
    const generatedAt = Date.now();
    try {
      const qr = await QRCode.toDataURL(rawQr, { width: 304, margin: 1 });
      if (shuttingDown || sessions.get(sessionId) !== state || state.retired || state.qrVersion !== version) return;
      state.status = 'qr_ready';
      state.qr = qr;
      state.qrGeneratedAt = generatedAt;
      if (!state.qrIdleStartedAt) state.qrIdleStartedAt = generatedAt;
      state.lastError = '';
      await callback(sessionId, 'qr');
    } catch (error) {
      if (sessions.get(sessionId) !== state || state.retired || state.qrVersion !== version) return;
      state.status = 'failed';
      state.qr = null;
      state.qrGeneratedAt = 0;
      state.lastError = 'Could not generate WhatsApp QR: ' + (error.message || String(error));
      state.restoreRetryAt = Date.now() + BASE_RETRY_MS;
      await callback(sessionId, 'failed', { error: state.lastError });
    }`);
  replace("    state.status = 'connecting';\n    state.qr = null;", "    state.qrVersion = Number(state.qrVersion || 0) + 1;\n    state.startupStartedAt = Date.now();\n    state.status = 'connecting';\n    state.qr = null;");
  replace("    state.lastError = String(message || 'Authentication failed');", "    state.qrVersion = Number(state.qrVersion || 0) + 1;\n    state.lastError = String(message || 'Authentication failed');");
  replace("    state.lastError = String(reason || 'Disconnected');", "    state.qrVersion = Number(state.qrVersion || 0) + 1;\n    state.callbackReady = false;\n    state.lastError = String(reason || 'Disconnected');");

  replace("  if (state.status === 'failed' || state.status === 'disconnected') return false;", `  if (shuttingDown || sessions.get(sessionId) !== state || state.retired || ['failed', 'expired'].includes(state.status)) return false;`);
  replace("  if (state.status === 'running') return true;", '');
  replace('    if (connectedDigits) state.phoneNumber = `+${connectedDigits}`;', `    // CONNECTED can precede WWebJS ClientInfo initialization. Do not bypass
    // the linked-number check or publish a ready session with no identity.
    if (!connectedDigits) return false;
    state.phoneNumber = \`+\${connectedDigits}\`;`);
  replace("    state.status = 'running';\n    state.qr = null;", "    state.reconnectRequired = false;\n    if (state.status === 'running') return true;\n    state.qrVersion = Number(state.qrVersion || 0) + 1;\n    state.status = 'running';\n    state.qr = null;");
  replace("    startHistorySync(sessionId, state).catch((error) => {", "    if (sessions.get(sessionId) !== state || state.retired || state.status !== 'running') return false;\n    startHistorySync(sessionId, state).catch((error) => {");
  replace(`    state.status = 'disconnected';
    state.restoreRetryAt = Date.now() + 5000;
    throw new Error('Hosted session is reconnecting; no message was sent.');`, `    state.status = 'connecting';
    state.reconnectRequired = true;
    state.callbackReady = false;
    state.startupStartedAt = Date.now();
    state.restoreRetryAt = Date.now() + 5000;
    await callback(sessionId, 'connecting');
    throw new Error('Hosted session is reconnecting; no message was sent.');`);

  replaceFunction('reconcileClientState', `async function reconcileClientState(sessionId, state) {
  if (!state || shuttingDown || sessions.get(sessionId) !== state || state.retired || ['failed', 'expired'].includes(state.status)) return;
  if (state.probePromise) return state.probePromise;
  // Status/QR polling and the restore loop also probe running clients so a
  // lost disconnected event cannot leave an unusable browser "running".
  if (state.status === 'running' && Date.now() - Number(state.lastProbeAt || 0) < 5000) return;
  state.lastProbeAt = Date.now();
  state.probePromise = (async () => {
    try {
      const waState = String(await deadline(() => state.client.getState(), 5000, 'Hosted session probe') || '').toUpperCase();
      if (sessions.get(sessionId) !== state || state.retired) return;
      if (waState === 'CONNECTED') {
        await promoteRunningSession(sessionId, state, 'state_probe');
      } else if (state.status === 'running') {
        state.status = 'connecting';
        state.reconnectRequired = true;
        state.callbackReady = false;
        state.startupStartedAt = Date.now();
        state.restoreRetryAt = Date.now() + 5000;
        state.lastError = 'WhatsApp connection lost: ' + (waState || 'unavailable');
        await callback(sessionId, 'connecting', { reason: state.lastError });
      }
    } catch (error) {
      if (sessions.get(sessionId) !== state || state.retired || state.status !== 'running') return;
      state.status = 'connecting';
      state.reconnectRequired = true;
      state.callbackReady = false;
      state.startupStartedAt = Date.now();
      state.restoreRetryAt = Date.now() + 5000;
      state.lastError = 'WhatsApp connection probe failed: ' + (error.message || String(error));
      await callback(sessionId, 'connecting', { reason: state.lastError });
    }
  })().finally(async () => {
    try {
      if (sessions.get(sessionId) !== state || state.retired || !['initializing', 'connecting'].includes(state.status)) return;
      if (!state.startupStartedAt || Date.now() - state.startupStartedAt < 120000) return;
      state.status = 'failed';
      state.qrVersion = Number(state.qrVersion || 0) + 1;
      state.qr = null;
      state.qrGeneratedAt = 0;
      state.lastError = 'WhatsApp session initialization timed out; reconnecting.';
      state.restoreRetryAt = Date.now() + BASE_RETRY_MS;
      await callback(sessionId, 'failed', { error: state.lastError });
    } finally { state.probePromise = null; }
  });
  return state.probePromise;
}`);

  // Reads, retries, logout and refresh must all share the same local lock;
  // a scan may otherwise destroy the new browser created by an HTTP request.
  replaceFunction('restoreSessions', `async function restoreSessions() {
  if (shuttingDown) return;
  const entries = await fs.promises.readdir(AUTH_PATH, { withFileTypes: true });
  for (const entry of entries) {
    if (!entry.isDirectory() || !entry.name.startsWith('session-')) continue;
    const sessionId = entry.name.slice('session-'.length);
    if (!sessionId) continue;
    try {
      await withSessionOperation(sessionId, async () => {
        if (shuttingDown) return;
        // Logout may have removed this profile since readdir completed.
        try { await fs.promises.access(sessionProfilePath(sessionId)); } catch (_) { return; }
        const existing = sessions.get(sessionId);
        if (existing?.status === 'expired') return;
        if (existing && (['failed', 'disconnected'].includes(existing.status) || existing.reconnectRequired)) {
          if (existing.restoreRetryAt > Date.now()) return;
          sessions.delete(sessionId);
          await destroyClientBounded(sessionId, existing, 'retry disconnected or failed restore');
          await releaseLock(sessionId).catch(() => {});
          await cleanSessionChromiumLocks(sessionId).catch(() => {});
        }
        await createSessionUnlocked(sessionId, existing?.requestedPhone || existing?.phoneNumber || '');
      });
    } catch (error) {
      console.warn('Could not restore session ' + sessionId + ':', error.message);
    }
  }
}`);

  // QR expiry is another profile mutation; serialize it with refresh/logout.
  replaceFunction('expireIdleQrSessions', `async function expireIdleQrSessions() {
  if (shuttingDown) return;
  for (const [sessionId, state] of Array.from(sessions.entries())) {
    if (state?.status !== 'qr_ready') continue;
    await withSessionOperation(sessionId, async () => {
      if (shuttingDown || sessions.get(sessionId) !== state || state.retired) return;
      if (state.status !== 'qr_ready' || !state.qrIdleStartedAt || Date.now() - state.qrIdleStartedAt < QR_IDLE_TIMEOUT_MS) return;
      state.status = 'expired';
      state.qr = null;
      state.qrGeneratedAt = 0;
      state.qrIdleStartedAt = 0;
      state.lastError = 'QR session expired after waiting too long for a scan. Refresh QR to reconnect.';
      await destroyClientBounded(sessionId, state, 'expire idle QR');
      try { await removeSessionProfile(sessionId); }
      catch (error) { console.warn('Could not clean expired QR LocalAuth ' + sessionId + ':', error.message); }
      await releaseLock(sessionId).catch(() => {});
      await callback(sessionId, 'disconnected', { reason: state.lastError });
    });
  }
}`);

  // A provider-rotated QR may have expired even though its image is cached.
  replace('    qr: state.qr,\n    expiresIn: state.qr ? Math.max(0, QR_EXPIRES_SECONDS - ageSeconds) : 0,',
    '    qr: ageSeconds < QR_EXPIRES_SECONDS ? state.qr : null,\n    expiresIn: state.qr ? Math.max(0, QR_EXPIRES_SECONDS - ageSeconds) : 0,');
  return source;
}

if (require.main === module) {
  const target = path.join(process.cwd(), 'src', 'index.js');
  const output = patchSource(fs.readFileSync(target, 'utf8'));
  new (require('vm').Script)(output, { filename: target });
  fs.writeFileSync(target, output);
  console.log('Hosted session lifecycle serialization and QR readiness patch complete.');
}
module.exports = { patchSource };
