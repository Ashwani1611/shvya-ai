'use strict';

// Run LAST, after the older source patches. Keep their fail-fast signatures
// intact while giving every lifecycle entry point one tested implementation.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const target = path.join(process.cwd(), 'src', 'index.js');
let source = fs.readFileSync(target, 'utf8');
const marker = '// SHVYA owned browser lifecycle v1';

function replace(before, after, alreadyApplied = () => false) {
  if (!source.includes(before)) {
    if (alreadyApplied()) return;
    throw new Error(`Lifecycle patch signature missing: ${before.slice(0, 100)}`);
  }
  if (source.indexOf(before) !== source.lastIndexOf(before)) {
    throw new Error(`Lifecycle patch signature missing or ambiguous: ${before.slice(0, 100)}`);
  }
  source = source.replace(before, () => after);
}
function functionText(name) {
  const match = new RegExp(`^(?:async )?function ${name}\\(`, 'm').exec(source);
  if (!match) throw new Error(`Missing lifecycle function ${name}`);
  const end = source.indexOf('\n}', match.index);
  if (end < 0) throw new Error(`Missing end of lifecycle function ${name}`);
  return source.slice(match.index, end + 2);
}
function replaceFunction(name, text) { replace(functionText(name), text); }

if (!source.includes(marker)) {
  // The current gateway source serializes createSession through a wrapper and
  // keeps the actual Client construction in createSessionUnlocked. Older
  // sources construct the Client directly in createSession.
  const createFunction = /^async function createSessionUnlocked\(/m.test(source)
    ? 'createSessionUnlocked'
    : 'createSession';
  const create = functionText(createFunction);
  const clientStart = create.indexOf('  const client = new Client({');
  const clientEnd = create.indexOf('  state.client = client;', clientStart);
  if (clientStart < 0 || clientEnd < 0) throw new Error('Cannot locate Client configuration');
  const clientFactory = create.slice(clientStart, clientEnd).trim()
    .replace('const client = new Client(', 'return new Client(')
    .replace('headless: true,', `headless: true,
      // Only the gateway owns process signals; one handler per browser leaks
      // listeners and can race the bounded multi-session shutdown.
      handleSIGTERM: false,
      handleSIGINT: false,
      handleSIGHUP: false,`);

  replace('const app = express();', `${marker}
const hostedLifecycle = require('./session-lifecycle').createHostedLifecycle({
  sessions, acquireLock, releaseLock, callback, wireClientEvents,
  reconcileClientState, removeSessionProfile, authPath: AUTH_PATH, maxSessions: MAX_HOSTED_SESSIONS,
  metrics: gatewayMetrics,
  createClient(sessionId) {
    ${clientFactory}
  },
  onUnsafeCleanup(error) {
    // init/tini exits with Node, then Docker terminates the namespace. Leave
    // unconfirmed leases untouched; do not permit a second profile owner.
    console.error('Unsafe Hosted browser cleanup; recycling gateway:', error.message);
    process.exit(1);
  },
});

const app = express();`);

  const adapters = {
    createSession: ["sessionId, requestedPhone = ''", 'hostedLifecycle.createSession(sessionId, requestedPhone)'],
    refreshQr: ['sessionId', 'hostedLifecycle.refreshQr(sessionId)'],
    logoutSession: ['sessionId', 'hostedLifecycle.logoutSession(sessionId)'],
    restoreSessions: ['', 'hostedLifecycle.restoreSessions()'],
    expireIdleQrSessions: ['', 'hostedLifecycle.expireIdleQrSessions(QR_IDLE_TIMEOUT_MS)'],
    fenceSession: ['sessionId, state, reason', 'hostedLifecycle.fenceSession(sessionId, state, reason)'],
    destroyClientBounded: ["sessionId, state, reason = 'destroy'", 'hostedLifecycle.stopClient(sessionId, state)'],
  };
  for (const [name, [args, call]] of Object.entries(adapters)) {
    replaceFunction(name, `async function ${name}(${args}) {\n  return ${call};\n}`);
  }

  replaceFunction('shutdown', `async function shutdown() {
  if (shuttingDown) return;
  shuttingDown = true;
  const deadline = setTimeout(() => {
    console.error('Hosted gateway shutdown deadline exceeded; preserving unconfirmed leases.');
    process.exit(1);
  }, 45000);
  try {
    await hostedLifecycle.shutdown();
    if (redis) await withTimeout(redis.quit(), 3000, 'Redis shutdown');
    process.exit(0);
  } catch (error) {
    console.error('Hosted gateway shutdown failed:', error.message);
    process.exit(1);
  } finally {
    clearTimeout(deadline);
  }
}`);

  replace(
    "    if (!state || state.status === 'failed' || state.status === 'expired') continue;",
    '    if (!state || state.stopped || sessions.get(sessionId) !== state) continue;',
  );
  const directCallback = functionText('directCallback');
  replaceFunction('directCallback', directCallback
    .replace('async function directCallback(sessionId, event, payload = {}) {',
      'async function directCallback(sessionId, event, payload = {}) {\n  if (!hostedLifecycle.callbackAllowed(sessionId)) return false;')
    .replace('  for (let attempt = 1; attempt <= 3; attempt += 1) {',
      '  for (let attempt = 1; attempt <= 3; attempt += 1) {\n    if (!hostedLifecycle.callbackAllowed(sessionId)) return false;'));

  const qrImageLine = '    state.qr = await QRCode.toDataURL(rawQr, { width: 304, margin: 1 });';
  if (source.includes(qrImageLine)) {
    replace(qrImageLine,
      `    const qr = await QRCode.toDataURL(rawQr, { width: 304, margin: 1 });
    if (!hostedLifecycle.current(sessionId, state) || state.status !== 'qr_ready') return;
    state.qr = qr;`);
  } else {
    replace('      if (shuttingDown || sessions.get(sessionId) !== state || state.retired || state.qrVersion !== version) return;',
      '      if (shuttingDown || !hostedLifecycle.current(sessionId, state) || state.qrVersion !== version) return;',
      () => source.includes('!hostedLifecycle.current(sessionId, state) || state.qrVersion !== version'));
  }
  replace(
    "async function promoteRunningSession(sessionId, state, source = 'ready') {",
    "async function promoteRunningSession(sessionId, state, source = 'ready') {\n  if (!hostedLifecycle.current(sessionId, state)) return false;",
  );
  replace(
    "    state.status = 'running';\n    state.qr = null;",
    "    state.status = 'running';\n    hostedLifecycle.markRunning(sessionId, state);\n    state.qr = null;",
  );
  replace(
    "    startHistorySync(sessionId, state).catch((error) => {\n      console.warn(`Could not sync hosted history for ${sessionId}:`, error.message);",
    "    if (!hostedLifecycle.current(sessionId, state)) return false;\n    startHistorySync(sessionId, state).catch((error) => {\n      console.warn(`Could not sync hosted history for ${sessionId}:`, error.message);",
  );
  replace(
    "      await callback(sessionId, 'failed', {\n        phoneNumber: state.phoneNumber,\n        error: state.lastError,\n      });\n      try { await client.logout(); } catch (_) {}",
    "      await hostedLifecycle.failSession(sessionId, state, 'failed', state.lastError, { logout: true });",
  );
  replace(
    'async function reconcileClientState(sessionId, state) {',
    'async function reconcileClientState(sessionId, state) {\n  if (!state || !hostedLifecycle.current(sessionId, state)) return;',
  );
  replace(
    '    lastError: state.lastError || \'\',',
    "    lastError: state.lastError || '',\n    retryPaused: Boolean(state.retryPaused),\n    retryAt: state.restoreRetryAt || null,",
  );
  const capacityBySessions = 'capacityRemaining: Math.max(0, MAX_HOSTED_SESSIONS - sessions.size)';
  if (source.includes(capacityBySessions)) {
    replace(capacityBySessions, 'capacityRemaining: Math.max(0, MAX_HOSTED_SESSIONS - hostedLifecycle.diagnostics().active)');
  } else {
    replace('capacityRemaining: Math.max(0, MAX_HOSTED_SESSIONS - activeSessionCount())',
      'capacityRemaining: Math.max(0, MAX_HOSTED_SESSIONS - hostedLifecycle.diagnostics().active)',
      () => source.includes('hostedLifecycle.diagnostics().active'));
  }
  replace(
    "app.use('/sessions', (req, res, next) => {",
    "app.use('/sessions', (req, res, next) => {\n  if (shuttingDown) return res.status(503).json({ error: 'Gateway is shutting down.' });",
  );
  replace(
    '  res.json({\n    ok: true,\n    shard: GATEWAY_SHARD,',
    '  res.status(shuttingDown ? 503 : 200).json({\n    ok: !shuttingDown,\n    lifecycle: hostedLifecycle.diagnostics(),\n    shard: GATEWAY_SHARD,',
  );
}

for (const required of [marker, 'handleSIGTERM: false', 'hostedLifecycle.shutdown()',
  'hostedLifecycle.createSession(sessionId, requestedPhone)', 'hostedLifecycle.callbackAllowed(sessionId)',
  'hostedLifecycle.expireIdleQrSessions(QR_IDLE_TIMEOUT_MS)', '45000']) {
  if (!source.includes(required)) throw new Error(`Lifecycle patch verification failed: ${required}`);
}
new vm.Script(source, { filename: target });
fs.writeFileSync(target, source);
console.log('Hosted owned-process lifecycle patch complete.');
