'use strict';

const fs = require('fs');
const path = require('path');
const marker = '// SHVYA_HOSTED_REALTIME_DELIVERY_V1';

function patchSource(input) {
  if (input.includes(marker)) return input;
  let source = input;
  function replace(before, after, expected = 1) {
    const count = source.split(before).length - 1;
    if (count !== expected) throw new Error(`Hosted realtime patch expected ${expected} matches, found ${count}: ${before.slice(0, 90)}`);
    source = source.split(before).join(after);
  }
  replace("const { idempotentSend } = require('./send-idempotency');", `const { idempotentSend: rawIdempotentSend } = require('./send-idempotency');
const { deadline, confirmSendOutcome, createCallbackOutbox } = require('./realtime-delivery');
${marker}`);
  replace('async function callback(sessionId, event, payload = {}) {',
    'async function directCallback(sessionId, event, payload = {}) {');
  replace('function lockKey(sessionId) {', `const callbackOutbox = createCallbackOutbox({
  root: path.join(AUTH_PATH, '_callback_outbox'),
  deliver: async (sessionId, event, payload) => {
    const state = sessions.get(sessionId);
    if (!state || state.status !== 'running') return false;
    // A lost ready callback must not make Django reject the first live AI job.
    // Re-announce current state, not a stale lifecycle event from the outbox.
    if (!state.callbackReady) {
      if (!state.callbackReadyPromise) {
        state.callbackReadyPromise = directCallback(sessionId, 'ready', {
          phoneNumber: state.phoneNumber, source: 'live_callback_recovery',
        }).then(ok => { state.callbackReady = ok; return ok; })
          .finally(() => { state.callbackReadyPromise = null; });
      }
      if (!(await state.callbackReadyPromise)) return false;
    }
    if (sessions.get(sessionId) !== state || state.status !== 'running') return false;
    const accepted = await directCallback(sessionId, event, payload);
    if (!accepted) state.callbackReady = false;
    return accepted;
  },
  isSessionReady: sessionId => !shuttingDown && sessions.get(sessionId)?.status === 'running',
});

async function callback(sessionId, event, payload = {}) {
  if (event === 'message' || event === 'message_ack') {
    return callbackOutbox.enqueue(sessionId, event, payload);
  }
  return directCallback(sessionId, event, payload);
}

async function idempotentSend(options) {
  const outcome = await rawIdempotentSend(options);
  return confirmSendOutcome(outcome, sessions.get(String(options.sessionId))?.client);
}

async function ensureHostedSendReady(sessionId, state) {
  if (shuttingDown || sessions.get(sessionId) !== state || ['failed', 'expired'].includes(state.status)) {
    throw new Error('Hosted session is unavailable.');
  }
  const current = String(await deadline(() => state.client.getState(), 5000, 'Hosted connection probe') || '').toUpperCase();
  if (current !== 'CONNECTED') {
    state.status = 'disconnected';
    state.restoreRetryAt = Date.now() + 5000;
    throw new Error('Hosted session is reconnecting; no message was sent.');
  }
  if (state.status !== 'running') {
    state.status = 'connecting';
    await promoteRunningSession(sessionId, state, 'send_probe');
  }
  if (state.status !== 'running') throw new Error('Hosted session is not running.');
}

function lockKey(sessionId) {`);
  replace('      resolvedChat = await message.getChat();',
    "      resolvedChat = await deadline(() => message.getChat(), 1500, 'Hosted live chat lookup');");
  replace("    const waState = String(await state.client.getState() || '').toUpperCase();",
    "    const waState = String(await deadline(() => state.client.getState(), 5000, 'Hosted session probe') || '').toUpperCase();");
  replace('  if (!sessions.has(sessionId)) return;', '  if (sessions.get(sessionId) !== state) return;');
  replace('  try { await state.client.destroy(); } catch (_) {}',
    "  await destroyClientBounded(sessionId, state, 'fence');");

  const eventStart = source.indexOf('function wireClientEvents(sessionId, state) {');
  const eventEnd = source.indexOf('\nasync function createSession(', eventStart);
  if (eventStart < 0 || eventEnd < 0) throw new Error('Hosted event block not found');
  let events = source.slice(eventStart, eventEnd);
  let guarded = 0;
  events = events.replace(/client\.on\('[^']+', async \([^)]*\) => \{/g, signature => {
    guarded += 1;
    return signature + '\n    if (shuttingDown || sessions.get(sessionId) !== state || state.status === \'expired\') return;';
  });
  if (guarded !== 9) throw new Error(`Expected nine Hosted event handlers, found ${guarded}`);
  const disconnected = "    await callback(sessionId, 'disconnected', { reason: state.lastError });";
  if (!events.includes(disconnected)) throw new Error('Hosted disconnected handler not found');
  events = events.replace(disconnected, `    const tracker = sessionFailureTrackers.get(sessionId) || { attempts: 0 };
    tracker.attempts += 1;
    sessionFailureTrackers.set(sessionId, tracker);
    state.restoreRetryAt = Date.now() + Math.min(5000 * (2 ** Math.min(tracker.attempts - 1, 12)), MAX_RETRY_MS);
${disconnected}`);
  source = source.slice(0, eventStart) + events + source.slice(eventEnd);

  const restoreStart = source.indexOf('async function restoreSessions() {');
  const restoreEnd = source.indexOf('\nasync function startRedis()', restoreStart);
  if (restoreStart < 0 || restoreEnd < 0) throw new Error('Hosted restore block not found');
  let restore = source.slice(restoreStart, restoreEnd);
  if (restore.split("existing.status === 'failed'").length - 1 !== 2) throw new Error('Hosted restore conditions changed');
  restore = restore.split("existing.status === 'failed'").join("['failed', 'disconnected'].includes(existing.status)");
  // Keep the configured sender identity when replacing a dead browser.
  restore = restore.replace('      await createSession(sessionId);',
    "      await createSession(sessionId, existing?.requestedPhone || existing?.phoneNumber || '');");
  source = source.slice(0, restoreStart) + restore + source.slice(restoreEnd);

  let preparations = 0;
  source = source.replace(/await reconcileClientState\(req\.params\.sessionId, state\);\n +if \(state\.status !== 'running'\) throw new Error\('Session is not running\.'\);/g, () => {
    preparations += 1;
    return 'await ensureHostedSendReady(req.params.sessionId, state);';
  });
  if (preparations !== 2) throw new Error(`Expected text and uploaded-media preparation paths, found ${preparations}`);
  replace('timestamp: sent.timestamp', 'timestamp: sent.timestamp, ack: sent.ack', 2);
  replace('          : await state.client.sendMessage(chatId, body);',
    '          : await state.client.sendMessage(chatId, body, { sendSeen: false });');
  replace('              caption: body || undefined,', '              sendSeen: false,\n              caption: body || undefined,');
  replace('            caption: caption || undefined,', '            sendSeen: false,\n            caption: caption || undefined,');
  replace("      heartbeatCallbacks.push(callback(sessionId, 'gateway_heartbeat'));",
    "      heartbeatCallbacks.push(callback(sessionId, state.status === 'running' ? 'running' : 'gateway_heartbeat', { phoneNumber: state.phoneNumber }));");
  replace('    await startRedis();', '    await startRedis();\n    callbackOutbox.start();');
  replace('  shuttingDown = true;', '  shuttingDown = true;\n  callbackOutbox.stop();');
  return source;
}

if (require.main === module) {
  const target = path.join(process.cwd(), 'src', 'index.js');
  const output = patchSource(fs.readFileSync(target, 'utf8'));
  // Refuse to publish an invalid assembled gateway source.
  new (require('vm').Script)(output, { filename: target });
  fs.writeFileSync(target, output);
  console.log('Hosted realtime delivery and acknowledgement patch complete.');
}
module.exports = { patchSource };
