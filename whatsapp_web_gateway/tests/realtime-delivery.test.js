'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { confirmSendOutcome, createCallbackOutbox, deadline } = require('../src/realtime-delivery');

const response = ack => ({ status: 201, body: { ok: true, messageId: 'provider-id', timestamp: 123, ack } });
async function until(predicate) {
  for (let i = 0; i < 200; i += 1) {
    if (await predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  assert.fail('Timed out waiting for test condition');
}
async function fixture(t, options = {}) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'hosted-outbox-'));
  const outbox = createCallbackOutbox({ root, isSessionReady: () => true, ...options });
  t.after(async () => { outbox.stop(); await outbox.flush(); await fs.rm(root, { recursive: true, force: true }); });
  return { root, outbox, records: async () => (await fs.readdir(root)).filter(name => name.endsWith('.json')) };
}

test('a local message ID and ACK_PENDING cannot report successful delivery', async () => {
  const result = await confirmSendOutcome(response(0), { getMessageById: async () => ({ ack: 0 }) });
  assert.equal(result.status, 503);
  assert.equal(result.body.code, 'provider_ack_pending');
  assert.equal(result.body.messageId, 'provider-id');
});
for (const [ack, status] of [[1, 'sent'], [2, 'delivered'], [3, 'read'], [4, 'read']]) {
  test(`server ACK ${ack} preserves ${status} status`, async () => {
    const result = await confirmSendOutcome(response(0), { getMessageById: async () => ({ ack }) });
    assert.equal(result.status, 201);
    assert.equal(result.body.status, status);
  });
}
test('provider rejection is not mistaken for a sent message', async () => {
  const result = await confirmSendOutcome(response(-1), null);
  assert.equal(result.status, 409);
  assert.equal(result.body.code, 'provider_rejected');
});
test('missing or stalled browser has a bounded, non-successful outcome', async () => {
  assert.equal((await confirmSendOutcome(response(undefined), null)).status, 503);
  const result = await confirmSendOutcome(response(0), { getMessageById: () => new Promise(() => {}) }, 10);
  assert.equal(result.body.code, 'provider_ack_pending');
});
test('a replay checks the same provider ID without generating another send', async () => {
  let sends = 0;
  let persisted;
  const rawIdempotentSend = async () => persisted || (sends += 1, persisted = response(0));
  const checked = [];
  let ack = 0;
  const client = { getMessageById: async id => { checked.push(id); return { ack }; } };
  assert.equal((await confirmSendOutcome(await rawIdempotentSend(), client)).status, 503);
  ack = 2;
  assert.equal((await confirmSendOutcome(await rawIdempotentSend(), client)).body.status, 'delivered');
  assert.equal(sends, 1);
  assert.deepEqual(checked, ['provider-id', 'provider-id']);
});
test('idempotency conflict/uncertain errors remain unchanged', async () => {
  const conflict = { status: 409, body: { code: 'provider_outcome_uncertain' } };
  assert.equal(await confirmSendOutcome(conflict, null), conflict);
});
test('lookup deadline also handles a synchronous provider exception', async () => {
  await assert.rejects(deadline(() => { throw new Error('bridge unavailable'); }, 50, 'lookup'), /bridge unavailable/);
});
test('failed callbacks persist privately and replay after process reconstruction', async t => {
  let now = 1000;
  let calls = 0;
  const { root, outbox, records } = await fixture(t, { clock: () => now, deliver: async () => { calls += 1; return false; } });
  await outbox.enqueue('account-a', 'message', { messageId: 'inbound-1', body: 'test' });
  await until(async () => {
    const names = await records();
    return names.length === 1 && JSON.parse(await fs.readFile(path.join(root, names[0]), 'utf8')).attempts === 1;
  });
  const recordPath = path.join(root, (await records())[0]);
  assert.equal((await fs.stat(recordPath)).mode & 0o777, 0o600);
  assert.equal((await fs.readdir(root)).filter(name => name.endsWith('.tmp')).length, 0);
  outbox.stop();
  now += 60000;
  const received = [];
  const restarted = createCallbackOutbox({ root, clock: () => now, isSessionReady: id => id === 'account-a',
    deliver: async (...args) => { received.push(args); return true; } });
  await restarted.flush();
  assert.equal(received.length, 1);
  assert.equal(received[0][2].messageId, 'inbound-1');
  assert.equal((await records()).length, 0);
  assert.equal(calls, 1);
});
test('callbacks wait for the owning session rather than crossing account boundaries', async t => {
  let ready = false;
  let calls = 0;
  const { outbox, records } = await fixture(t, {
    isSessionReady: id => ready && id === 'account-a',
    deliver: async () => { calls += 1; return true; },
  });
  await outbox.enqueue('account-a', 'message_ack', { messageId: 'outbound-1', status: 'read' });
  await new Promise(resolve => setTimeout(resolve, 20));
  await outbox.flush();
  assert.equal(calls, 0);
  assert.equal((await records()).length, 1);
  ready = true;
  await outbox.flush();
  assert.equal(calls, 1);
  assert.equal((await records()).length, 0);
});
test('duplicate callbacks share one active durable record', async t => {
  let resolveSend;
  const gate = new Promise(resolve => { resolveSend = resolve; });
  let calls = 0;
  const { outbox, records } = await fixture(t, { deliver: async () => { calls += 1; await gate; return true; } });
  await Promise.all(Array.from({ length: 8 }, () => outbox.enqueue('account', 'message', { messageId: 'same-id' })));
  await until(() => calls === 1);
  assert.equal((await records()).length, 1);
  resolveSend();
  await until(async () => (await records()).length === 0);
  assert.equal(calls, 1);
});
