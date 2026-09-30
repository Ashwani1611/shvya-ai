'use strict';

// Redis owns cross-process claims. The existing persistent session volume keeps
// the send journal across a Redis/container restart; never replay an uncertain
// provider call. No customer text/media is written to either store.
const crypto = require('crypto');
const fs = require('fs/promises');
const path = require('path');

const RETENTION_SECONDS = 7 * 24 * 60 * 60;
const PENDING_SECONDS = 300;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const cleaned = new Map();
const digest = value => crypto.createHash('sha256').update(value).digest('hex');

function failure(status, code, error, retryAfter) {
  return { status, body: { error, code }, ...(retryAfter ? { retryAfter } : {}) };
}

function replay(record, fingerprint, now) {
  if (record.fingerprint !== fingerprint) {
    return failure(409, 'request_payload_conflict', 'Send request ID was reused with different content.');
  }
  if (record.status === 'completed') return { status: 201, body: record.response };
  if (record.status === 'pending' && now - record.startedAt < PENDING_SECONDS * 1000) {
    return failure(503, 'request_pending', 'This send request is still processing.', 30);
  }
  return failure(409, 'provider_outcome_uncertain', 'Delivery outcome is uncertain; automatic replay is blocked.');
}

async function readJournal(file) {
  try { return JSON.parse(await fs.readFile(file, 'utf8')); }
  catch (error) { if (error.code === 'ENOENT') return null; throw error; }
}

async function writeJournal(file, record, exclusive = false) {
  const target = exclusive ? file : `${file}.${crypto.randomUUID()}.tmp`;
  const handle = await fs.open(target, 'wx', 0o600);
  try {
    try { await handle.writeFile(JSON.stringify(record)); await handle.sync(); }
    finally { await handle.close(); }
    if (!exclusive) await fs.rename(target, file);
    const directory = await fs.open(path.dirname(file), 'r');
    try { await directory.sync(); } finally { await directory.close(); }
  } catch (error) {
    await fs.unlink(target).catch(() => {});
    throw error;
  }
}

async function cleanup(directory, now) {
  if (now - (cleaned.get(directory) || 0) < 60 * 60 * 1000) return;
  cleaned.set(directory, now);
  // Retain unknown attempts for the full retry horizon too. Scan every entry
  // in bounded batches in the background so newer files cannot hide old ones.
  const names = await fs.readdir(directory);
  for (let offset = 0; offset < names.length; offset += 100) {
    await Promise.all(names.slice(offset, offset + 100).map(async name => {
      const file = path.join(directory, name);
      const stat = await fs.stat(file).catch(() => null);
      if (stat && stat.isFile() && now - stat.mtimeMs > RETENTION_SECONDS * 1000) {
        await fs.unlink(file).catch(() => {});
      }
    }));
  }
}

async function idempotentSend({ redis, journalRoot, sessionId, requestId, requestIsRetry = false, payload, prepare, send, now = Date.now() }) {
  // Older/manual callers remain compatible. AI callers always supply their
  // durable WhatsAppMessage UUID and may not fall back to an unguarded send.
  if (!requestId) return { status: 201, body: await send(prepare ? await prepare() : undefined) };
  if (!UUID.test(String(requestId))) return failure(400, 'invalid_request_id', 'Invalid send request ID.');
  const fingerprint = digest(JSON.stringify(payload));
  const identity = `${String(sessionId)}:${String(requestId).toLowerCase()}`;
  const key = `shvya:wwebjs:send:v1:${identity}`;
  const directory = path.join(journalRoot, digest(String(sessionId)));
  const file = path.join(directory, `${String(requestId).toLowerCase()}.json`);
  const record = { status: 'pending', fingerprint, token: crypto.randomUUID(), startedAt: now };
  let claimed = false;
  let journalCreated = false;
  async function releaseUnsent() {
    if (!claimed) return;
    await redis.eval(
      "local v=redis.call('get',KEYS[1]); if v and cjson.decode(v).token==ARGV[1] then "
      + "return redis.call('del',KEYS[1]) end; return 0",
      { keys: [key], arguments: [record.token] },
    ).catch(() => {});
    if (journalCreated) await fs.unlink(file).catch(() => {});
  }
  try {
    await fs.mkdir(directory, { recursive: true, mode: 0o700 });
    void cleanup(directory, now).catch(() => {});
    const persisted = await readJournal(file);
    if (persisted) {
      // Completion may have reached only Redis after a disk write failure.
      // Never let an older pending journal hide a confirmed provider result.
      if (persisted.fingerprint === fingerprint && persisted.status !== 'completed' && redis && redis.isReady !== false) {
        try {
          const cached = JSON.parse(await redis.get(key) || 'null');
          if (cached && cached.token === persisted.token && cached.fingerprint === fingerprint
              && cached.status === 'completed' && cached.response && cached.response.messageId) {
            return replay(cached, fingerprint, now);
          }
        } catch (_) { /* Keep the durable journal's fail-closed outcome. */ }
      }
      return replay(persisted, fingerprint, now);
    }
    if (!redis || redis.isReady === false) throw new Error('Send claim store unavailable');
    if (requestIsRetry) {
      const previous = await redis.get(key);
      if (previous) return replay(JSON.parse(previous), fingerprint, now);
      return failure(409, 'provider_outcome_uncertain', 'Previous send claim is unavailable; automatic replay is blocked.');
    }
    const acquired = await redis.set(key, JSON.stringify(record), { NX: true, EX: RETENTION_SECONDS });
    if (acquired !== 'OK') {
      const existing = await redis.get(key);
      if (!existing) return failure(503, 'send_claim_unavailable', 'Send claim temporarily unavailable.', 30);
      return replay(JSON.parse(existing), fingerprint, now);
    }
    claimed = true;
    try { await writeJournal(file, record, true); journalCreated = true; }
    catch (error) {
      if (error.code === 'EEXIST') return replay(await readJournal(file), fingerprint, now);
      throw error;
    }
  } catch (_) {
    // This branch is before provider I/O, so release only our own claim rather
    // than leaving a known-unsent request permanently pending.
    await releaseUnsent();
    return failure(503, 'send_claim_unavailable', 'Send claim storage is unavailable; no message was sent.', 30);
  }

  let prepared;
  try { prepared = prepare ? await prepare() : undefined; }
  catch (_) {
    await releaseUnsent();
    return failure(503, 'send_claim_unavailable', 'Message preparation failed; no message was sent.', 30);
  }

  async function finalize(updated) {
    // At least one store must retain the outcome; an existing pending journal
    // prevents a repeat even if neither completion write can finish.
    const writes = await Promise.allSettled([
      writeJournal(file, updated),
      redis.eval(
        "local v=redis.call('get',KEYS[1]); if v and cjson.decode(v).token==ARGV[1] then "
        + "return redis.call('set',KEYS[1],ARGV[2],'EX',ARGV[3]) end; return 0",
        { keys: [key], arguments: [record.token, JSON.stringify(updated), String(RETENTION_SECONDS)] },
      ),
    ]);
    return writes.some(result => result.status === 'fulfilled' && result.value !== 0);
  }

  try {
    const response = await send(prepared);
    if (!response || !response.messageId) throw new Error('Provider returned no message ID');
    await finalize({ ...record, status: 'completed', response });
    return { status: 201, body: response };
  } catch (_) {
    await finalize({ ...record, status: 'uncertain' });
    return failure(409, 'provider_outcome_uncertain', 'Delivery outcome is uncertain; automatic replay is blocked.');
  }
}

module.exports = { idempotentSend, RETENTION_SECONDS, PENDING_SECONDS };
