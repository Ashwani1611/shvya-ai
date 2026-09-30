'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs/promises');
const os = require('os');
const path = require('path');
const { idempotentSend, PENDING_SECONDS } = require('../src/send-idempotency');

class MemoryRedis {
  constructor() { this.rows = new Map(); this.isReady = true; }
  async get(key) { return this.rows.get(key) || null; }
  async set(key, value, options) {
    if (options.NX && this.rows.has(key)) return null;
    this.rows.set(key, value);
    return 'OK';
  }
  async eval(script, { keys: [key], arguments: args }) {
    const existing = JSON.parse(this.rows.get(key) || 'null');
    if (!existing || existing.token !== args[0]) return 0;
    if (script.includes("redis.call('del'")) return Number(this.rows.delete(key));
    this.rows.set(key, args[1]);
    return 'OK';
  }
}

async function setup(t) {
  const journalRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'shvya-send-test-'));
  t.after(() => fs.rm(journalRoot, { recursive: true, force: true }));
  return {
    redis: new MemoryRedis(), journalRoot, sessionId: 'account-1',
    requestId: 'f4a6da38-d672-46e3-a93b-bf5f74141896',
    payload: ['chat', 'test body', 'text'],
  };
}

test('completed retry returns identical provider result without another send', async t => {
  const args = await setup(t);
  let sends = 0;
  const send = async () => { sends += 1; return { ok: true, messageId: 'provider-1', timestamp: 123 }; };
  const first = await idempotentSend({ ...args, send });
  const repeat = await idempotentSend({ ...args, send, requestIsRetry: true });
  assert.equal(first.status, 201);
  assert.deepEqual(repeat, first);
  assert.equal(sends, 1);
});

test('concurrent request is deferred while the owner is in provider I/O', async t => {
  const args = await setup(t);
  let release;
  let notifyStarted;
  const started = new Promise(resolve => { notifyStarted = resolve; });
  const provider = new Promise(resolve => { release = resolve; });
  let sends = 0;
  const send = async () => { sends += 1; notifyStarted(); await provider; return { messageId: 'provider-2' }; };
  const first = idempotentSend({ ...args, send });
  await started;
  const duplicate = await idempotentSend({ ...args, send });
  assert.equal(duplicate.status, 503);
  assert.equal(duplicate.body.code, 'request_pending');
  assert.equal(duplicate.retryAfter, 30);
  release();
  assert.equal((await first).status, 201);
  assert.equal(sends, 1);
});

test('persistent journal recovers result after Redis and worker restart', async t => {
  const args = await setup(t);
  await idempotentSend({ ...args, send: async () => ({ messageId: 'persisted-id' }) });
  const restarted = await idempotentSend({
    ...args, redis: new MemoryRedis(), requestIsRetry: true,
    send: async () => assert.fail('completed request must not be sent after restart'),
  });
  assert.equal(restarted.status, 201);
  assert.equal(restarted.body.messageId, 'persisted-id');
  const files = await fs.readdir(args.journalRoot, { recursive: true });
  const contents = await fs.readFile(path.join(args.journalRoot, files.find(file => file.endsWith('.json'))), 'utf8');
  assert.equal(contents.includes('test body'), false);
  assert.equal(contents.includes('chat'), false);
});

test('Redis completed result recovers across different gateway storage', async t => {
  const args = await setup(t);
  const alternate = await setup(t);
  await idempotentSend({ ...args, send: async () => ({ messageId: 'shared-id' }) });
  const replayed = await idempotentSend({
    ...args, journalRoot: alternate.journalRoot, requestIsRetry: true,
    send: async () => assert.fail('cross-host retry must reuse completed Redis result'),
  });
  assert.equal(replayed.body.messageId, 'shared-id');
});

test('Redis completion supersedes stale pending journal only for the same claim', async t => {
  const args = await setup(t);
  await idempotentSend({ ...args, send: async () => ({ messageId: 'redis-completed' }) });
  const files = await fs.readdir(args.journalRoot, { recursive: true });
  const file = path.join(args.journalRoot, files.find(name => name.endsWith('.json')));
  const record = JSON.parse(await fs.readFile(file, 'utf8'));
  const pending = { ...record, status: 'pending' };
  delete pending.response;
  await fs.writeFile(file, JSON.stringify(pending));
  const recovered = await idempotentSend({ ...args, requestIsRetry: true,
    send: async () => assert.fail('Redis completion must avoid another send') });
  assert.equal(recovered.body.messageId, 'redis-completed');

  await fs.writeFile(file, JSON.stringify({ ...pending, token: 'different-claim' }));
  const differentClaim = await idempotentSend({ ...args, requestIsRetry: true,
    send: async () => assert.fail('another claim must not take ownership') });
  assert.equal(differentClaim.status, 503);
  assert.equal(differentClaim.body.code, 'request_pending');
});

test('unknown retry on another host fails closed when both stores lost history', async t => {
  const args = await setup(t);
  const outcome = await idempotentSend({
    ...args, requestIsRetry: true,
    send: async () => assert.fail('unknown previous outcome must not be replayed'),
  });
  assert.equal(outcome.status, 409);
  assert.equal(outcome.body.code, 'provider_outcome_uncertain');
});

test('provider exception remains uncertain and never causes a duplicate send', async t => {
  const args = await setup(t);
  let sends = 0;
  const send = async () => { sends += 1; throw new Error('browser failed after dispatch'); };
  const failed = await idempotentSend({ ...args, send });
  const retry = await idempotentSend({ ...args, send });
  assert.equal(failed.status, 409);
  assert.equal(retry.body.code, 'provider_outcome_uncertain');
  assert.equal(sends, 1);
});

test('stale in-flight attempt is uncertain, never reclaimable', async t => {
  const args = await setup(t);
  let finish;
  let started;
  const notify = new Promise(resolve => { started = resolve; });
  const pending = new Promise(resolve => { finish = resolve; });
  const now = Date.now();
  const first = idempotentSend({ ...args, now, send: async () => {
    started(); await pending; return { messageId: 'eventually-sent' };
  } });
  await notify;
  const expired = await idempotentSend({ ...args, now: now + (PENDING_SECONDS + 1) * 1000,
    send: async () => assert.fail('stale claim is not proof of failed delivery') });
  assert.equal(expired.status, 409);
  finish();
  await first;
});

test('request ID rejects changed content and scopes identical IDs by account', async t => {
  const args = await setup(t);
  let sends = 0;
  const send = async () => ({ messageId: `id-${++sends}` });
  await idempotentSend({ ...args, send });
  const conflict = await idempotentSend({ ...args, payload: ['different content'], send });
  assert.equal(conflict.body.code, 'request_payload_conflict');
  const separate = await idempotentSend({ ...args, sessionId: 'account-2', send });
  assert.equal(separate.status, 201);
  assert.equal(sends, 2);
});

test('unavailable Redis prevents a new provider send without leaving a disk claim', async t => {
  const args = await setup(t);
  const unavailable = await idempotentSend({ ...args, redis: null,
    send: async () => assert.fail('new send needs a shared claim') });
  assert.equal(unavailable.status, 503);
  const recovered = await idempotentSend({ ...args, send: async () => ({ messageId: 'after-recovery' }) });
  assert.equal(recovered.status, 201);
});

test('completed retry does not redownload an expired media URL', async t => {
  const args = await setup(t);
  const first = await idempotentSend({ ...args,
    prepare: async () => 'file content',
    send: async media => { assert.equal(media, 'file content'); return { messageId: 'media-id' }; },
  });
  const retry = await idempotentSend({ ...args, requestIsRetry: true,
    prepare: async () => assert.fail('completed sends must not fetch media again'),
    send: async () => assert.fail('completed sends must not send media again'),
  });
  assert.deepEqual(retry, first);
});

test('failure before provider dispatch releases the claim for a safe retry', async t => {
  const args = await setup(t);
  const failed = await idempotentSend({ ...args,
    prepare: async () => { throw new Error('media temporarily unavailable'); },
    send: async () => assert.fail('preparation failed before dispatch'),
  });
  assert.equal(failed.body.code, 'send_claim_unavailable');
  const retried = await idempotentSend({ ...args, send: async () => ({ messageId: 'retry-id' }) });
  assert.equal(retried.status, 201);
});
