const test = require('node:test');
const assert = require('node:assert/strict');
const { registerGroupOperations, exactGroupId, boundedLimit } = require('../src/group-operations');

const groupId = '120363123456789@g.us';
const member = '919876543210@c.us';

function harness({ isMember = true, status = 'running' } = {}) {
  const routes = new Map();
  const sent = [];
  const chat = { id: { _serialized: groupId }, name: 'Client support', isGroup: true,
    participants: [{ id: { _serialized: isMember ? member : 'other@c.us' } }],
    fetchMessages: async ({ limit }) => Array.from({ length: limit }, (_, n) => ({
      id: { _serialized: `msg-${n}` }, author: '919123456789@c.us', body: 'Client requirements', timestamp: n,
    })),
  };
  const state = { status, client: { info: { wid: { _serialized: member } },
    getChats: async () => [chat, { ...chat, id: { _serialized: member }, isGroup: false }],
    getChatById: async () => chat,
    sendMessage: async (target, body) => { sent.push({ target, body }); return { id: { _serialized: 'sent-1' }, timestamp: 1 }; },
  } };
  const journal = new Map();
  registerGroupOperations({
    get: (url, fn) => routes.set(`GET ${url}`, fn), post: (url, fn) => routes.set(`POST ${url}`, fn),
  }, { sessions: new Map([['sender', state]]), reconcileClientState: async () => {},
    serializedId: id => id?._serialized || '', withTimeout: async promise => promise,
    idempotentSend: async args => {
      const fingerprint = JSON.stringify(args.payload);
      if (journal.has(args.requestId)) {
        const existing = journal.get(args.requestId);
        return existing.fingerprint === fingerprint ? existing.result : { status: 409, body: { error: 'Changed payload' } };
      }
      await args.prepare();
      const result = { status: 200, body: await args.send() };
      journal.set(args.requestId, { fingerprint, result });
      return result;
    }, redis: null, journalRoot: '/unused',
  });
  async function call(method, suffix, payload = {}) {
    const response = { code: 200, status(code) { this.code = code; return this; },
      json(body) { this.body = body; return this; }, set() {} };
    await routes.get(`${method} /sessions/:sessionId/${suffix}`)({
      params: { sessionId: 'sender', groupId }, query: {}, body: {}, ...payload,
    }, response);
    return response;
  }
  return { call, sent, chat, state };
}

test('exact group target rejects phone numbers, names and URL paths', () => {
  for (const value of ['Client group', '919876543210', '12@c.us', '../groups', '1@g.us?x=1', null]) {
    assert.throws(() => exactGroupId(value));
  }
  assert.equal(exactGroupId('123-456@g.us'), '123-456@g.us');
  assert.throws(() => boundedLimit(101));
});

test('group discovery excludes direct chats and nonmembers', async () => {
  let h = harness();
  let response = await h.call('GET', 'groups');
  assert.equal(response.body.groups.length, 1);
  assert.equal(response.body.groups[0].id, groupId);
  h = harness({ isMember: false });
  response = await h.call('GET', 'groups');
  assert.deepEqual(response.body.groups, []);
});

test('group read requires current membership and bounded history', async () => {
  let h = harness();
  let response = await h.call('GET', 'groups/:groupId/messages', { query: { limit: '2' } });
  assert.equal(response.body.messages.length, 2);
  assert.equal(h.sent.length, 0);
  h = harness({ isMember: false });
  response = await h.call('GET', 'groups/:groupId/messages');
  assert.equal(response.code, 403);
});

test('group send preserves exact body/recipient and journals approval request', async () => {
  const h = harness();
  const body = { body: 'Gaurav: Approved support update', requestId: '12345678-1234-1234-1234-123456789abc' };
  assert.equal((await h.call('POST', 'groups/:groupId/messages', { body })).code, 200);
  assert.equal((await h.call('POST', 'groups/:groupId/messages', { body })).code, 200);
  assert.deepEqual(h.sent, [{ target: groupId, body: body.body }]);
  assert.equal((await h.call('POST', 'groups/:groupId/messages', { body: { ...body, body: 'changed' } })).code, 409);
  assert.equal(h.sent.length, 1);
});

test('group send rejects missing request ID, nonmembers and disconnected sessions', async () => {
  const body = { body: 'Approved message', requestId: '12345678-1234-1234-1234-123456789abc' };
  const h = harness();
  assert.equal((await h.call('POST', 'groups/:groupId/messages', { body: { body: 'Hi' } })).code, 400);
  assert.equal(h.sent.length, 0);
  for (const options of [{ isMember: false }, { status: 'disconnected' }]) {
    const other = harness(options);
    assert.ok((await other.call('POST', 'groups/:groupId/messages', { body })).code >= 400);
    assert.equal(other.sent.length, 0);
  }
});
