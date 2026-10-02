'use strict';

const crypto = require('crypto');
const fs = require('fs/promises');
const path = require('path');

function deadline(operation, milliseconds, label) {
  let timer;
  return Promise.race([
    Promise.resolve().then(operation),
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`${label} timed out`)), milliseconds);
    }),
  ]).finally(() => clearTimeout(timer));
}

// A local WWebJS message ID is NOT a server acknowledgement. Reconcile the
// existing provider message on retries; never call sendMessage a second time.
async function confirmSendOutcome(outcome, client, timeoutMs = 2500) {
  if (!outcome || outcome.status !== 201 || !outcome.body?.messageId) return outcome;
  const body = outcome.body;
  let ack = Number.isInteger(body.ack) ? body.ack : null;
  if (ack === null || ack === 0) {
    try {
      const message = await deadline(
        () => client.getMessageById(body.messageId), timeoutMs, 'Hosted send acknowledgement',
      );
      if (Number.isInteger(message?.ack)) ack = message.ack;
    } catch (_) { /* An unavailable browser is not proof of delivery. */ }
  }
  if (ack !== null && ack >= 1) {
    return { ...outcome, body: { ...body, ack, status: ack >= 3 ? 'read' : ack === 2 ? 'delivered' : 'sent' } };
  }
  if (ack !== null && ack < 0) {
    return {
      status: 409,
      body: { ...body, ok: false, ack, code: 'provider_rejected', error: 'WhatsApp rejected this message.' },
    };
  }
  return {
    status: 503, retryAfter: 5,
    body: {
      ...body, ok: false, ack, code: 'provider_ack_pending',
      error: 'Awaiting WhatsApp server acknowledgement; the existing message will be checked without resending.',
    },
  };
}

// Live messages and ACKs survive web-container outages and gateway restarts.
// Files live on the existing private LocalAuth volume, not in public media.
function createCallbackOutbox({ root, deliver, isSessionReady, log = console.warn,
  clock = Date.now, intervalMs = 2000, concurrency = 4 }) {
  let timer = null;
  let draining = null;
  const active = new Set();
  let liveInFlight = 0;

  async function write(file, record, exclusive = false) {
    // Publish only a fully synced file. A crash must never expose partial JSON.
    const temporary = `${file}.${crypto.randomUUID()}.tmp`;
    try {
      const handle = await fs.open(temporary, 'wx', 0o600);
      try {
        await handle.writeFile(JSON.stringify(record));
        await handle.sync();
      } finally { await handle.close(); }
      if (exclusive) await fs.link(temporary, file);
      else await fs.rename(temporary, file);
      const directory = await fs.open(root, 'r');
      try { await directory.sync(); } finally { await directory.close(); }
    } finally { await fs.unlink(temporary).catch(() => {}); }
  }

  async function attempt(file) {
    if (active.has(file)) return;
    active.add(file);
    try {
      const record = JSON.parse(await fs.readFile(file, 'utf8'));
      if (!isSessionReady(record.sessionId) || record.nextAt > clock()) return;
      let delivered = false;
      try { delivered = await deliver(record.sessionId, record.event, record.payload); }
      catch (_) { /* Preserve the durable record for the next attempt. */ }
      if (delivered) {
        await fs.unlink(file);
      } else {
        record.attempts += 1;
        record.nextAt = clock() + Math.min(60000, 1000 * (2 ** Math.min(record.attempts, 6)));
        await write(file, record);
      }
    } catch (error) {
      if (error.code !== 'ENOENT') log('Hosted callback outbox attempt failed:', error.message);
    } finally { active.delete(file); }
  }

  function flush() {
    if (draining) return draining;
    draining = (async () => {
      await fs.mkdir(root, { recursive: true, mode: 0o700 });
      // Streaming directory iteration bounds memory even during a long outage.
      const directory = await fs.opendir(root);
      const batch = [];
      for await (const entry of directory) {
        if (!entry.isFile() || !entry.name.endsWith('.json')) continue;
        batch.push(attempt(path.join(root, entry.name)));
        if (batch.length >= concurrency) {
          await Promise.all(batch);
          batch.length = 0;
        }
      }
      await Promise.all(batch);
    })().catch(error => log('Hosted callback outbox scan failed:', error.message))
      .finally(() => { draining = null; });
    return draining;
  }

  async function enqueue(sessionId, event, payload) {
    const identity = JSON.stringify([sessionId, event, payload.messageId, payload.status || '']);
    const filename = crypto.createHash('sha256').update(identity).digest('hex') + '.json';
    try {
      await fs.mkdir(root, { recursive: true, mode: 0o700 });
      await write(path.join(root, filename), {
        sessionId, event, payload, attempts: 0, createdAt: clock(), nextAt: 0,
      }, true);
    } catch (error) {
      if (error.code !== 'EEXIST') {
        log('Hosted callback could not be persisted:', error.message);
        return deliver(sessionId, event, payload);
      }
    }
    // Reserve a separate bounded lane for new live events: an old backlog
    // must not make a fresh message wait behind slow retry callbacks.
    if (liveInFlight < concurrency) {
      liveInFlight += 1;
      void attempt(path.join(root, filename)).finally(() => { liveInFlight -= 1; });
    } else {
      void flush();
    }
    return true;
  }

  function start() {
    if (timer) return;
    timer = setInterval(() => { void flush(); }, intervalMs);
    timer.unref();
    void flush();
  }
  function stop() { clearInterval(timer); timer = null; }
  return { enqueue, flush, start, stop };
}

module.exports = { deadline, confirmSendOutcome, createCallbackOutbox };
