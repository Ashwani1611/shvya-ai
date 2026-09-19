'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const { execFileSync } = require('node:child_process');

// Exercise the same ordered source transformations as the production image.
const root = path.resolve(__dirname, '..');
const work = fs.mkdtempSync(path.join(os.tmpdir(), 'hosted-sync-tests-'));
fs.cpSync(path.join(root, 'src'), path.join(work, 'src'), { recursive: true });
fs.cpSync(path.join(root, 'scripts'), path.join(work, 'scripts'), { recursive: true });
const dockerfile = fs.readFileSync(path.join(root, 'Dockerfile'), 'utf8');
for (const match of dockerfile.matchAll(/^RUN node (scripts\/patch-hosted\S+)/gm)) {
  execFileSync(process.execPath, [match[1]], { cwd: work, stdio: 'pipe' });
}
const source = fs.readFileSync(path.join(work, 'src/index.js'), 'utf8');
execFileSync(process.execPath, ['--check', 'src/index.js'], { cwd: work });
fs.rmSync(work, { recursive: true });

function functionSource(name) {
  const start = source.search(new RegExp(`(?:async )?function ${name}\\(`));
  assert.ok(start >= 0, `Missing production function ${name}`);
  const tail = source.slice(start);
  const end = tail.search(/\n(?:async )?function /);
  return end < 0 ? tail : tail.slice(0, end);
}
function context(names, overrides = {}) {
  const sandbox = {
    console: { warn() {}, log() {} },
    HISTORY_GET_CHATS_TIMEOUT_MS: 30000, HISTORY_CHAT_FETCH_TIMEOUT_MS: 15000,
    HISTORY_CHAT_INDEX_LIMIT: 1000, HISTORY_DEEP_CHAT_LIMIT: 100,
    HISTORY_MESSAGE_LIMIT: 100, HISTORY_INDEX_MESSAGE_LIMIT: 1,
    HISTORY_SYNC_CONCURRENCY: 3, LID_RESOLVE_BATCH_SIZE: 50,
    serializedId: value => typeof value === 'string' ? value : value?._serialized || '',
    withTimeout: promise => promise,
    callback: async () => true,
    resolveLidPhoneMap: async () => new Map(),
    sessions: new Map(), clearTimeout() {},
    setTimeout: () => ({ unref() {} }),
    ...overrides,
  };
  vm.createContext(sandbox);
  vm.runInContext(['isHiddenHostedChatId', ...names].map(functionSource).join('\n'), sandbox);
  return sandbox;
}

test('production patch chain applies and sync orders before truncating the chat index', async () => {
  const visited = [];
  const identities = [];
  const chats = Array.from({ length: 1005 }, (_, i) => ({ id: `${i}@c.us`, timestamp: i }));
  chats.push({ id: 'status@broadcast', timestamp: 99999 });
  const ctx = context(['syncRecentHistory'], {
    syncOneChat: async (_session, chat, _client, options) => {
      visited.push([chat.timestamp, options.messageLimit]);
      return { chats: 1, messages: 1 };
    },
    resolveLidPhoneMap: async (_client, ids) => { identities.push(ids); return new Map(); },
  });
  const result = await ctx.syncRecentHistory('session', { client: { getChats: async () => chats } });
  assert.equal(result.chats, 1000);
  assert.deepEqual(visited.slice(0, 3).map(row => row[0]), [1004, 1003, 1002]);
  assert.equal(Math.min(...visited.map(row => row[0])), 5);
  assert.equal(visited.filter(row => row[1] === 100).length, 100);
  assert.ok(identities.every(ids => ids.length <= 50));
});

test('failed chat does not stop other imports or falsely complete history', async () => {
  const visited = [];
  const ctx = context(['syncRecentHistory'], {
    syncOneChat: async (_id, chat) => {
      visited.push(chat.id);
      if (chat.id === 'bad@c.us') throw new Error('fetch failed');
      return { chats: 1, messages: 1 };
    },
  });
  const state = { client: { getChats: async () => [
    { id: 'good@c.us', timestamp: 3 }, { id: 'bad@c.us', timestamp: 2 },
    { id: 'last@c.us', timestamp: 1 },
  ] } };
  await assert.rejects(ctx.syncRecentHistory('session', state), /incomplete/);
  assert.equal(state.historyFailedChats, 1);
  assert.equal(visited.length, 3);
});

test('history callbacks are capped at 20 and failed delivery is surfaced', async () => {
  const sizes = [];
  const ctx = context(['sendHistoryBatch'], {
    callback: async (_id, _event, payload) => { sizes.push(payload.messages.length); return true; },
  });
  await ctx.sendHistoryBatch('session', Array.from({ length: 51 }, (_, id) => ({ id })));
  assert.deepEqual(sizes, [20, 20, 11]);
  ctx.callback = async () => false;
  await assert.rejects(ctx.sendHistoryBatch('session', [{}]), /Django rejected/);
});

test('only newest incoming history messages count as unread', async () => {
  const messages = [
    { id: 'old', timestamp: 1, fromMe: false },
    { id: 'out', timestamp: 4, fromMe: true },
    { id: 'new', timestamp: 3, fromMe: false },
  ];
  let sent;
  let requested;
  const ctx = context(['syncOneChat'], {
    resolveChatIdentity: async () => ({ rawChatId: 'peer@c.us' }),
    serializeMessage: async message => ({ ...message }),
    sendHistoryBatch: async (_id, batch) => { sent = batch; },
    phoneFromId: () => '',
  });
  await ctx.syncOneChat('session', {
    id: 'peer@c.us', unreadCount: 1,
    fetchMessages: async ({ limit }) => { requested = limit; return messages; },
  }, {}, { messageLimit: 10 });
  assert.equal(requested, 10);
  assert.deepEqual(Array.from(sent, item => [item.id, item.isUnread]), [
    ['out', false], ['new', true], ['old', false],
  ]);
});

test('history fetch failure is not silently reported as zero successful messages', async () => {
  const ctx = context(['syncOneChat'], {
    resolveChatIdentity: async () => ({}), phoneFromId: () => '',
  });
  await assert.rejects(ctx.syncOneChat('session', {
    id: 'peer@c.us', fetchMessages: async () => { throw new Error('timeout'); },
  }, {}), /timeout/);
});

test('sync retries are bounded, deduplicated and fenced to the original running session', async () => {
  const timers = [];
  const sessions = new Map();
  const ctx = context(['startHistorySync'], {
    syncRecentHistory: async () => { throw new Error('temporary'); }, sessions,
    setTimeout: (fn, delay) => { timers.push({ fn, delay }); return { unref() {} }; },
  });
  const state = { status: 'running' };
  sessions.set('session', state);
  const first = ctx.startHistorySync('session', state);
  assert.equal(first, ctx.startHistorySync('session', state));
  await assert.rejects(first, /temporary/);
  for (let i = 0; i < 3; i += 1) {
    assert.equal(timers[i].delay, 5000 * (2 ** i));
    timers[i].fn();
    await assert.rejects(state.historySyncPromise, /temporary/);
  }
  assert.equal(timers.length, 3);
  assert.equal(state.historySynced, false);
  assert.match(state.historyError, /temporary/);
});

test('live LID message is forwarded when message.getChat cannot resolve the LID', async () => {
  const lid = '109698229481999@lid';
  const phone = '+919811223344';
  const requested = [];
  const ctx = context([
    'digits', 'phoneFromId', 'phoneFromContact', 'mapMessageType', 'ackStatus',
    'resolveMessageIdentity', 'serializeMessage',
  ], {
    CONTACT_LOOKUP_TIMEOUT_MS: 5000,
    resolveMessageBody: async (message) => message.body || '',
    resolveLidPhoneMap: async (_client, ids) => {
      requested.push(...ids);
      return new Map([[lid, phone]]);
    },
  });
  const message = {
    id: 'LID-LIVE-1',
    from: lid,
    to: '918700274739@c.us',
    fromMe: false,
    body: 'Hello',
    type: 'chat',
    timestamp: 1_725_000_100,
    ack: 0,
    author: '',
    getChat: async () => { throw new Error('No LID for user'); },
    getContact: async () => ({ id: lid, pushname: 'Live LID Prospect' }),
  };

  const serialized = await ctx.serializeMessage(message, null, null, {});

  assert.deepEqual(requested, [lid]);
  assert.equal(serialized.rawChatId, lid);
  assert.equal(serialized.peerKey, phone);
  assert.equal(serialized.peerPhone, phone);
  assert.equal(serialized.contactPhoneNumber, phone);
  assert.equal(serialized.from, '919811223344@c.us');
  assert.equal(serialized.contactName, 'Live LID Prospect');
  assert.equal(serialized.isGroup, false);
});

test('WhatsApp public pushname takes precedence over a saved address-book nickname', async () => {
  const ctx = context(['resolveChatIdentity'], {
    CONTACT_LOOKUP_TIMEOUT_MS: 5000, phoneFromContact: () => '+919812345678',
    phoneFromId: () => '+919812345678',
  });
  const identity = await ctx.resolveChatIdentity({
    id: '919812345678@c.us', name: 'Chat Name',
    getContact: async () => ({ name: 'Saved Nickname', pushname: 'Public Profile', id: '919812345678@c.us' }),
  });
  assert.equal(identity.contactName, 'Public Profile');
  assert.equal(identity.profileName, 'Public Profile');
});

test('manual sync acknowledges queued work instead of holding the HTTP request open', () => {
  const route = source.slice(source.indexOf("app.post('/sessions/:sessionId/sync'"), source.indexOf("app.get('/sessions/:sessionId/existing-chats'"));
  assert.match(route, /res\.status\(202\)/);
  assert.doesNotMatch(route, /await startHistorySync/);
});
