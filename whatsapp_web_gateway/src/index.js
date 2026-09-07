'use strict';

const crypto = require('crypto');
const fs = require('fs');
const express = require('express');
const QRCode = require('qrcode');
const { createClient: createRedisClient } = require('redis');
const {
  Client,
  LocalAuth,
  MessageMedia,
} = require('whatsapp-web.js');

const PORT = Number(process.env.PORT || 3000);
const AUTH_PATH = process.env.WHATSAPP_WEB_SESSION_PATH || '/data/wwebjs_auth';
const API_TOKEN = process.env.WHATSAPP_WEB_GATEWAY_TOKEN || '';
const CALLBACK_TOKEN = process.env.WHATSAPP_WEB_CALLBACK_TOKEN || '';
const CALLBACK_URL = process.env.SHVYA_HOSTED_CALLBACK_URL || '';
const REDIS_URL = process.env.REDIS_URL || '';
const INSTANCE_ID = crypto.randomUUID();
const QR_EXPIRES_SECONDS = 60;
const HISTORY_CHAT_LIMIT = 100;
const HISTORY_MESSAGE_LIMIT = 30;
const HISTORY_SYNC_CONCURRENCY = 4;
const HISTORY_CHAT_FETCH_TIMEOUT_MS = 12000;
const HISTORY_GET_CHATS_TIMEOUT_MS = 20000;
const EXISTING_CHATS_TIMEOUT_MS = 60000;
const CONTACT_LOOKUP_TIMEOUT_MS = 5000;
const LID_RESOLVE_BATCH_SIZE = 20;
const LID_RESOLVE_TIMEOUT_MS = 10000;
const LOCAL_SEND_MATCH_MS = 20000;

fs.mkdirSync(AUTH_PATH, { recursive: true });

const sessions = new Map();
let redis = null;

function digits(value) {
  return String(value || '').replace(/\D/g, '');
}

function serializedId(value) {
  if (!value) return '';
  if (typeof value === 'string') return value;
  if (value._serialized) return String(value._serialized);
  if (value.$1) return String(value.$1);
  if (value.user && value.server) return `${value.user}@${value.server}`;
  return '';
}

function phoneFromId(value) {
  const raw = serializedId(value);
  if (!raw.endsWith('@c.us')) return '';
  const number = digits(raw.split('@', 1)[0]);
  if (number.length < 8 || number.length > 15) return '';
  return `+${number}`;
}

function phoneFromContact(contact) {
  const contactId = serializedId(contact && contact.id);
  if (contactId.endsWith('@lid')) return '';
  const number = digits(contact && contact.number);
  if (number.length >= 8 && number.length <= 15) return `+${number}`;
  return phoneFromId(contact && contact.id);
}

function phoneFromPn(value) {
  const fromId = phoneFromId(value);
  if (fromId) return fromId;

  const raw = serializedId(value) || String(value || '');
  if (!raw || raw.includes('@lid')) return '';
  const number = digits(raw);
  if (number.length < 8 || number.length > 15) return '';
  return `+${number}`;
}

function publicSession(sessionId, state) {
  return {
    sessionId,
    status: state.status,
    phoneNumber: state.phoneNumber || state.requestedPhone || '',
    lastError: state.lastError || '',
    historySyncing: Boolean(state.historySyncPromise),
    historySynced: Boolean(state.historySynced),
    historyError: state.historyError || '',
    historyResult: state.historyResult || null,
  };
}

async function callback(sessionId, event, payload = {}) {
  if (!CALLBACK_URL || !CALLBACK_TOKEN) return false;
  try {
    const response = await fetch(CALLBACK_URL, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-SHVYA-Hosted-Token': CALLBACK_TOKEN,
      },
      body: JSON.stringify({ sessionId, event, ...payload }),
      signal: AbortSignal.timeout(10000),
    });
    if (!response.ok) {
      console.warn(`Hosted callback ${event} for ${sessionId} returned ${response.status}`);
      return false;
    }
    return true;
  } catch (error) {
    console.warn(`Hosted callback ${event} for ${sessionId} failed:`, error.message);
    return false;
  }
}

function lockKey(sessionId) {
  return `shvya:wwebjs:session:${sessionId}`;
}

async function acquireLock(sessionId) {
  if (!redis) return true;
  const key = lockKey(sessionId);
  const current = await redis.get(key);
  if (current === INSTANCE_ID) {
    await redis.expire(key, 90);
    return true;
  }
  const result = await redis.set(key, INSTANCE_ID, { NX: true, EX: 90 });
  return result === 'OK';
}

async function renewLocks() {
  if (!redis) return;
  for (const sessionId of sessions.keys()) {
    const key = lockKey(sessionId);
    const current = await redis.get(key);
    if (current === INSTANCE_ID) await redis.expire(key, 90);
  }
}

async function releaseLock(sessionId) {
  if (!redis) return;
  const key = lockKey(sessionId);
  const current = await redis.get(key);
  if (current === INSTANCE_ID) await redis.del(key);
}

function mapMessageType(type) {
  if (type === 'image') return 'image';
  if (type === 'audio' || type === 'ptt') return 'audio';
  if (type === 'video') return 'video';
  if (type === 'document') return 'document';
  return 'text';
}

function ackStatus(ack) {
  if (ack < 0) return 'failed';
  if (ack >= 3) return 'read';
  if (ack === 2) return 'delivered';
  if (ack === 1) return 'sent';
  return '';
}

function withTimeout(promise, timeoutMs, label) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} timed out`)), timeoutMs);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

async function resolveLidPhoneMap(client, lidIds) {
  const uniqueIds = [...new Set(
    (lidIds || [])
      .map((value) => serializedId(value))
      .filter((value) => value.endsWith('@lid')),
  )];
  const resolved = new Map();

  if (!uniqueIds.length) return resolved;
  if (!client || typeof client.getContactLidAndPhone !== 'function') {
    return resolved;
  }

  for (let offset = 0; offset < uniqueIds.length; offset += LID_RESOLVE_BATCH_SIZE) {
    const batch = uniqueIds.slice(offset, offset + LID_RESOLVE_BATCH_SIZE);
    try {
      const mappings = await withTimeout(
        client.getContactLidAndPhone(batch),
        LID_RESOLVE_TIMEOUT_MS,
        `resolve ${batch.length} LID contact(s)`,
      );
      for (const mapping of Array.isArray(mappings) ? mappings : []) {
        const lid = serializedId(mapping && mapping.lid);
        const phone = phoneFromPn(mapping && mapping.pn);
        if (lid && phone) resolved.set(lid, phone);
      }
    } catch (error) {
      console.warn('Could not resolve LID contact batch:', error.message);
    }
  }

  return resolved;
}

async function resolveChatIdentity(chat, client = null, lidPhoneMap = null) {
  const rawChatId = serializedId(chat && chat.id);
  const isGroup = Boolean(chat && (chat.isGroup || rawChatId.endsWith('@g.us')));
  if (isGroup) {
    return {
      rawChatId,
      peerKey: rawChatId,
      peerPhone: '',
      contactName: (chat && chat.name) || rawChatId,
      isGroup: true,
    };
  }

  let contact = null;
  try {
    contact = await withTimeout(
      chat.getContact(),
      CONTACT_LOOKUP_TIMEOUT_MS,
      `getContact ${rawChatId}`,
    );
  } catch (_) {}

  const contactId = serializedId(contact && contact.id);
  const lidId = rawChatId.endsWith('@lid')
    ? rawChatId
    : (contactId.endsWith('@lid') ? contactId : '');

  let peerPhone = '';
  if (lidId) {
    peerPhone = (lidPhoneMap && lidPhoneMap.get(lidId)) || '';
    if (!peerPhone && client) {
      const one = await resolveLidPhoneMap(client, [lidId]);
      peerPhone = one.get(lidId) || '';
    }
  } else {
    peerPhone = phoneFromContact(contact) || phoneFromId(rawChatId);
  }

  const contactName = (
    contact && (
      contact.name ||
      contact.pushname ||
      contact.shortName ||
      contact.verifiedName
    )
  ) || (chat && chat.name) || peerPhone || rawChatId;

  return {
    rawChatId,
    peerKey: peerPhone || rawChatId,
    peerPhone,
    contactName,
    isGroup: false,
  };
}

async function serializeMessage(message, chat = null, identity = null, client = null) {
  const resolvedChat = chat || await message.getChat();
  const resolvedIdentity = identity || await resolveChatIdentity(resolvedChat, client);
  const fromMe = Boolean(message.fromMe);
  let from = serializedId(message.from) || String(message.from || '');
  let to = serializedId(message.to) || String(message.to || '');

  // Direct chats may be represented by a privacy @lid. Django must receive
  // the real phone identity when it can be resolved, never the LID digits.
  if (!resolvedIdentity.isGroup && resolvedIdentity.peerPhone) {
    const peerId = `${digits(resolvedIdentity.peerPhone)}@c.us`;
    if (fromMe) to = peerId;
    else from = peerId;
  }

  return {
    messageId: serializedId(message.id),
    from,
    to,
    fromMe,
    body: message.body || '',
    messageType: mapMessageType(message.type),
    timestamp: message.timestamp,
    status: ackStatus(message.ack),
    chatId: resolvedIdentity.rawChatId,
    rawChatId: resolvedIdentity.rawChatId,
    peerKey: resolvedIdentity.peerKey,
    peerPhone: resolvedIdentity.peerPhone,
    contactPhoneNumber: resolvedIdentity.peerPhone,
    chatName: (resolvedChat && resolvedChat.name) || resolvedIdentity.contactName,
    isGroup: resolvedIdentity.isGroup,
    contactName: resolvedIdentity.contactName,
    author: message.author || '',
  };
}

async function sendHistoryBatch(sessionId, messages) {
  if (!messages.length) return;
  const delivered = await callback(sessionId, 'history_sync', { messages });
  if (!delivered) {
    throw new Error('Django rejected or did not receive hosted history batch');
  }
}

async function syncOneChat(sessionId, chat, client) {
  const rawChatId = serializedId(chat && chat.id);
  if (!rawChatId || rawChatId === 'status@broadcast' || rawChatId.includes('@newsletter')) {
    return { chats: 0, messages: 0 };
  }

  let identity;
  try {
    identity = await resolveChatIdentity(chat, client);
  } catch (error) {
    console.warn(`Could not resolve chat identity for ${sessionId}/${rawChatId}:`, error.message);
    identity = {
      rawChatId,
      peerKey: rawChatId,
      peerPhone: phoneFromId(rawChatId),
      contactName: chat.name || rawChatId,
      isGroup: Boolean(chat.isGroup),
    };
  }

  let messages;
  try {
    messages = await withTimeout(
      chat.fetchMessages({ limit: HISTORY_MESSAGE_LIMIT }),
      HISTORY_CHAT_FETCH_TIMEOUT_MS,
      `fetchMessages ${rawChatId}`,
    );
  } catch (error) {
    console.warn(`Could not fetch history for ${sessionId}/${rawChatId}:`, error.message);
    return { chats: 1, messages: 0 };
  }

  const batch = [];
  for (const message of messages) {
    if (!message || !serializedId(message.id)) continue;
    try {
      batch.push(await serializeMessage(message, chat, identity, client));
    } catch (error) {
      console.warn(`Could not serialize history for ${sessionId}/${rawChatId}:`, error.message);
    }
  }

  if (batch.length) await sendHistoryBatch(sessionId, batch);
  return { chats: 1, messages: batch.length };
}

async function syncRecentHistory(sessionId, state) {
  const chats = await withTimeout(
    state.client.getChats(),
    HISTORY_GET_CHATS_TIMEOUT_MS,
    'getChats',
  );
  const selectedChats = chats.slice(0, HISTORY_CHAT_LIMIT);
  let cursor = 0;
  let syncedMessages = 0;
  let syncedChats = 0;

  async function worker() {
    while (true) {
      const index = cursor++;
      if (index >= selectedChats.length) return;
      const result = await syncOneChat(
        sessionId,
        selectedChats[index],
        state.client,
      );
      syncedChats += result.chats;
      syncedMessages += result.messages;
    }
  }

  const workers = Array.from(
    { length: Math.min(HISTORY_SYNC_CONCURRENCY, selectedChats.length || 1) },
    () => worker(),
  );
  await Promise.all(workers);
  return { chats: syncedChats, messages: syncedMessages };
}

async function listExistingDirectChats(state) {
  const chats = await withTimeout(
    state.client.getChats(),
    EXISTING_CHATS_TIMEOUT_MS,
    'getChats for existing-chat snapshot',
  );

  const directChats = chats.filter((chat) => {
    if (!chat) return false;
    const chatId = serializedId(chat.id);
    if (!chatId) return false;
    if (chat.isGroup || chatId.endsWith('@g.us')) return false;
    if (chatId === 'status@broadcast' || chatId.includes('@newsletter')) return false;
    if (chatId.endsWith('@broadcast')) return false;
    return true;
  });

  const lidIds = directChats
    .map((chat) => serializedId(chat.id))
    .filter((chatId) => chatId.endsWith('@lid'));
  const lidPhoneMap = await resolveLidPhoneMap(state.client, lidIds);

  const byPhone = new Map();
  let unresolved = 0;
  const ownPhone = phoneFromPn(state.phoneNumber || state.requestedPhone);

  for (const chat of directChats) {
    let identity;
    try {
      identity = await resolveChatIdentity(chat, state.client, lidPhoneMap);
    } catch (error) {
      console.warn(
        `Could not resolve existing chat ${serializedId(chat.id)}:`,
        error.message,
      );
      identity = null;
    }

    if (!identity || !identity.peerPhone) {
      unresolved += 1;
      continue;
    }
    if (ownPhone && identity.peerPhone === ownPhone) continue;

    const previous = byPhone.get(identity.peerPhone);
    if (!previous || (!previous.contactName && identity.contactName)) {
      byPhone.set(identity.peerPhone, {
        chatId: identity.rawChatId,
        phoneNumber: identity.peerPhone,
        contactName: identity.contactName || identity.peerPhone,
        isGroup: false,
      });
    }
  }

  return {
    chats: Array.from(byPhone.values()),
    unresolved,
  };
}

function startHistorySync(sessionId, state, { force = false } = {}) {
  if (state.historySyncPromise) return state.historySyncPromise;
  if (state.historySynced && !force) {
    return Promise.resolve(state.historyResult || { chats: 0, messages: 0 });
  }

  state.historyError = '';
  state.historySyncPromise = syncRecentHistory(sessionId, state)
    .then(async (result) => {
      state.historySynced = true;
      state.historyResult = result;
      state.historyError = '';
      console.log(
        `Synced hosted history for ${sessionId}: ${result.chats} chats, ${result.messages} messages`,
      );
      await callback(sessionId, 'history_complete', result);
      return result;
    })
    .catch(async (error) => {
      state.historySynced = false;
      state.historyError = error.message || String(error);
      await callback(sessionId, 'history_failed', { error: state.historyError });
      throw error;
    })
    .finally(() => {
      state.historySyncPromise = null;
    });

  return state.historySyncPromise;
}

async function promoteRunningSession(sessionId, state, source = 'ready') {
  if (state.status === 'failed' || state.status === 'disconnected') return false;
  if (state.readyPromise) return state.readyPromise;
  if (state.status === 'running') return true;

  state.readyPromise = (async () => {
    const client = state.client;
    const connectedDigits = digits(client.info && client.info.wid && client.info.wid.user);
    if (connectedDigits) state.phoneNumber = `+${connectedDigits}`;

    const requested = digits(state.requestedPhone);
    const connected = digits(state.phoneNumber);
    if (requested && connected && requested !== connected) {
      state.status = 'failed';
      state.qr = null;
      state.qrGeneratedAt = 0;
      state.lastError = 'Scanned WhatsApp number does not match the pipeline-linked number.';
      await callback(sessionId, 'failed', {
        phoneNumber: state.phoneNumber,
        error: state.lastError,
      });
      try { await client.logout(); } catch (_) {}
      return false;
    }

    state.status = 'running';
    state.qr = null;
    state.qrGeneratedAt = 0;
    state.lastError = '';
    await callback(sessionId, 'ready', {
      phoneNumber: state.phoneNumber,
      source,
    });

    startHistorySync(sessionId, state).catch((error) => {
      console.warn(`Could not sync hosted history for ${sessionId}:`, error.message);
    });
    return true;
  })().finally(() => {
    state.readyPromise = null;
  });

  return state.readyPromise;
}

async function reconcileClientState(sessionId, state) {
  if (!state || state.status === 'running' || state.status === 'failed' || state.status === 'disconnected') {
    return;
  }
  try {
    const waState = String(await state.client.getState() || '').toUpperCase();
    if (waState === 'CONNECTED') {
      await promoteRunningSession(sessionId, state, 'state_probe');
    }
  } catch (_) {}
}

function pruneLocalSendState(state) {
  const now = Date.now();
  for (const token of state.localSendTokens) {
    if (now - token.startedAt > LOCAL_SEND_MATCH_MS) state.localSendTokens.delete(token);
  }
  for (const [messageId, expiresAt] of state.localMessageIds.entries()) {
    if (expiresAt <= now) state.localMessageIds.delete(messageId);
  }
}

function sameRecipient(a, b) {
  if (!a || !b) return false;
  if (a === b) return true;
  const aDigits = digits(a);
  const bDigits = digits(b);
  return aDigits && bDigits && aDigits === bDigits;
}

function isGatewayOriginatedOwnMessage(state, message) {
  pruneLocalSendState(state);
  const messageId = serializedId(message && message.id);
  if (messageId && state.localMessageIds.has(messageId)) return true;

  const to = serializedId(message && message.to) || String((message && message.to) || '');
  const body = String((message && message.body) || '');
  for (const token of state.localSendTokens) {
    if (!sameRecipient(token.chatId, to)) continue;
    if (!token.media && token.body !== body) continue;
    if (messageId) state.localMessageIds.set(messageId, Date.now() + 30000);
    token.matched = true;
    return true;
  }
  return false;
}

function wireClientEvents(sessionId, state) {
  const client = state.client;

  client.on('qr', async (rawQr) => {
    state.status = 'qr_ready';
    state.qr = await QRCode.toDataURL(rawQr, { width: 304, margin: 1 });
    state.qrGeneratedAt = Date.now();
    state.lastError = '';
    await callback(sessionId, 'qr');
  });

  client.on('authenticated', async () => {
    state.status = 'connecting';
    state.qr = null;
    state.qrGeneratedAt = 0;
    await callback(sessionId, 'authenticated');
    setTimeout(() => reconcileClientState(sessionId, state), 2000).unref();
    setTimeout(() => reconcileClientState(sessionId, state), 6000).unref();
  });

  client.on('auth_failure', async (message) => {
    state.status = 'failed';
    state.qr = null;
    state.qrGeneratedAt = 0;
    state.lastError = String(message || 'Authentication failed');
    await callback(sessionId, 'auth_failure', { error: state.lastError });
  });

  client.on('ready', async () => {
    await promoteRunningSession(sessionId, state, 'ready_event');
  });

  client.on('change_state', async (waState) => {
    if (String(waState || '').toUpperCase() === 'CONNECTED') {
      await promoteRunningSession(sessionId, state, 'change_state');
    }
  });

  // Incoming messages arrive on `message`. Keep this separate from
  // `message_create` to avoid delivering inbound events twice.
  client.on('message', async (message) => {
    if (message.fromMe) return;
    try {
      await callback(
        sessionId,
        'message',
        await serializeMessage(message, null, null, client),
      );
    } catch (error) {
      console.warn(`Could not forward inbound message for ${sessionId}:`, error.message);
    }
  });

  // `message_create` includes messages sent from the linked phone and other
  // companion devices. Suppress only messages that this gateway itself just
  // sent through the HTTP API; their queued DB row is updated elsewhere.
  client.on('message_create', async (message) => {
    if (!message.fromMe || isGatewayOriginatedOwnMessage(state, message)) return;
    try {
      await callback(
        sessionId,
        'message',
        await serializeMessage(message, null, null, client),
      );
    } catch (error) {
      console.warn(`Could not forward linked-device outbound for ${sessionId}:`, error.message);
    }
  });

  client.on('message_ack', async (message, ack) => {
    if (!message.fromMe) return;
    const status = ackStatus(ack);
    const messageId = serializedId(message.id);
    if (!status || !messageId) return;
    await callback(sessionId, 'message_ack', {
      messageId,
      status,
    });
  });

  client.on('disconnected', async (reason) => {
    state.status = 'disconnected';
    state.qr = null;
    state.qrGeneratedAt = 0;
    state.lastError = String(reason || 'Disconnected');
    await callback(sessionId, 'disconnected', { reason: state.lastError });
  });
}

async function createSession(sessionId, requestedPhone = '') {
  if (!/^[-_\w]+$/i.test(sessionId)) throw new Error('Invalid session id.');

  const existing = sessions.get(sessionId);
  if (existing) {
    if (requestedPhone) existing.requestedPhone = requestedPhone;
    await reconcileClientState(sessionId, existing);
    return existing;
  }

  if (!(await acquireLock(sessionId))) {
    const error = new Error('Session is active on another gateway instance.');
    error.statusCode = 409;
    throw error;
  }

  const state = {
    client: null,
    status: 'initializing',
    qr: null,
    qrGeneratedAt: 0,
    phoneNumber: '',
    requestedPhone,
    lastError: '',
    historySyncPromise: null,
    historySynced: false,
    historyResult: null,
    historyError: '',
    readyPromise: null,
    localSendTokens: new Set(),
    localMessageIds: new Map(),
  };

  const client = new Client({
    authStrategy: new LocalAuth({
      clientId: sessionId,
      dataPath: AUTH_PATH,
    }),
    puppeteer: {
      headless: true,
      executablePath: process.env.PUPPETEER_EXECUTABLE_PATH || undefined,
      args: [
        '--no-sandbox',
        '--disable-setuid-sandbox',
        '--disable-dev-shm-usage',
        '--disable-gpu',
      ],
    },
  });

  state.client = client;
  sessions.set(sessionId, state);
  wireClientEvents(sessionId, state);

  client.initialize().catch(async (error) => {
    state.status = 'failed';
    state.qr = null;
    state.qrGeneratedAt = 0;
    state.lastError = error.message || String(error);
    await callback(sessionId, 'failed', { error: state.lastError });
  });

  return state;
}

async function refreshQr(sessionId) {
  const current = sessions.get(sessionId);
  if (!current) return createSession(sessionId);
  await reconcileClientState(sessionId, current);
  if (current.status === 'running') return current;

  const requestedPhone = current.requestedPhone;
  try { await current.client.destroy(); } catch (_) {}
  sessions.delete(sessionId);
  await releaseLock(sessionId).catch(() => {});
  return createSession(sessionId, requestedPhone);
}

async function logoutSession(sessionId) {
  const state = sessions.get(sessionId);
  if (!state) {
    await releaseLock(sessionId);
    return;
  }
  try { await state.client.logout(); } catch (_) {}
  try { await state.client.destroy(); } catch (_) {}
  sessions.delete(sessionId);
  await releaseLock(sessionId);
  await callback(sessionId, 'logout');
}

async function restoreSessions() {
  const entries = await fs.promises.readdir(AUTH_PATH, { withFileTypes: true });
  for (const entry of entries) {
    if (!entry.isDirectory() || !entry.name.startsWith('session-')) continue;
    const sessionId = entry.name.slice('session-'.length);
    if (!sessionId) continue;
    try {
      await createSession(sessionId);
    } catch (error) {
      console.warn(`Could not restore session ${sessionId}:`, error.message);
    }
  }
}

async function startRedis() {
  if (!REDIS_URL) return;
  redis = createRedisClient({ url: REDIS_URL });
  redis.on('error', (error) => console.warn('Redis gateway lock error:', error.message));
  await redis.connect();
  setInterval(() => renewLocks().catch(() => {}), 30000).unref();
}

const app = express();
app.disable('x-powered-by');
app.use(express.json({ limit: '6mb' }));

app.get('/health', (_req, res) => res.json({ ok: true, sessions: sessions.size }));

app.use('/sessions', (req, res, next) => {
  if (!API_TOKEN) return res.status(503).json({ error: 'Gateway token is not configured.' });
  const expected = `Bearer ${API_TOKEN}`;
  const supplied = req.get('Authorization') || '';
  const a = Buffer.from(expected);
  const b = Buffer.from(supplied);
  if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) {
    return res.status(401).json({ error: 'Unauthorized' });
  }
  next();
});

app.post('/sessions', async (req, res) => {
  try {
    const sessionId = String(req.body.sessionId || '').trim();
    const phoneNumber = String(req.body.phoneNumber || '').trim();
    if (!sessionId || !phoneNumber) {
      return res.status(400).json({ error: 'sessionId and phoneNumber are required.' });
    }
    const state = await createSession(sessionId, phoneNumber);
    return res.status(202).json(publicSession(sessionId, state));
  } catch (error) {
    return res.status(error.statusCode || 500).json({ error: error.message });
  }
});

app.get('/sessions/:sessionId', async (req, res) => {
  const state = sessions.get(req.params.sessionId);
  if (!state) return res.status(404).json({ error: 'Session not found.' });
  await reconcileClientState(req.params.sessionId, state);
  return res.json(publicSession(req.params.sessionId, state));
});

app.get('/sessions/:sessionId/qr', async (req, res) => {
  const state = sessions.get(req.params.sessionId);
  if (!state) return res.status(404).json({ error: 'Session not found.' });
  await reconcileClientState(req.params.sessionId, state);
  const ageSeconds = state.qrGeneratedAt
    ? Math.floor((Date.now() - state.qrGeneratedAt) / 1000)
    : 0;
  return res.json({
    ...publicSession(req.params.sessionId, state),
    qr: state.qr,
    expiresIn: state.qr ? Math.max(0, QR_EXPIRES_SECONDS - ageSeconds) : 0,
  });
});

app.post('/sessions/:sessionId/refresh-qr', async (req, res) => {
  try {
    const state = await refreshQr(req.params.sessionId);
    return res.status(202).json(publicSession(req.params.sessionId, state));
  } catch (error) {
    return res.status(error.statusCode || 500).json({ error: error.message });
  }
});

app.post('/sessions/:sessionId/sync', async (req, res) => {
  const state = sessions.get(req.params.sessionId);
  if (!state) return res.status(404).json({ error: 'Session not found.' });
  await reconcileClientState(req.params.sessionId, state);
  if (state.status !== 'running') {
    return res.status(409).json({ error: 'Session is not running.' });
  }
  try {
    const result = await startHistorySync(req.params.sessionId, state, { force: true });
    return res.json({ ok: true, ...result });
  } catch (error) {
    return res.status(502).json({ error: error.message || String(error) });
  }
});

app.get('/sessions/:sessionId/existing-chats', async (req, res) => {
  const state = sessions.get(req.params.sessionId);
  if (!state) return res.status(404).json({ error: 'Session not found.' });
  await reconcileClientState(req.params.sessionId, state);
  if (state.status !== 'running') {
    return res.status(409).json({ error: 'Session is not running.' });
  }

  try {
    const result = await listExistingDirectChats(state);
    return res.json({
      ok: true,
      total: result.chats.length,
      unresolved: result.unresolved,
      chats: result.chats,
    });
  } catch (error) {
    return res.status(502).json({ error: error.message || String(error) });
  }
});

app.post('/sessions/:sessionId/messages', async (req, res) => {
  const state = sessions.get(req.params.sessionId);
  if (!state) return res.status(404).json({ error: 'Session not found.' });
  await reconcileClientState(req.params.sessionId, state);
  if (state.status !== 'running') {
    return res.status(409).json({ error: 'Session is not running.' });
  }

  const to = String(req.body.to || '').trim();
  const body = String(req.body.body || '');
  if (!to) return res.status(400).json({ error: 'Recipient is required.' });
  const chatId = to.includes('@') ? to : `${digits(to)}@c.us`;
  if (!chatId || chatId === '@c.us') {
    return res.status(400).json({ error: 'Invalid recipient.' });
  }

  const token = {
    chatId,
    body,
    media: Boolean(req.body.mediaUrl),
    startedAt: Date.now(),
    matched: false,
  };
  state.localSendTokens.add(token);

  try {
    let sent;
    if (req.body.mediaUrl) {
      const media = await MessageMedia.fromUrl(String(req.body.mediaUrl), {
        unsafeMime: false,
        filename: req.body.filename || undefined,
      });
      sent = await state.client.sendMessage(chatId, media, {
        caption: body || undefined,
      });
    } else {
      if (!body.trim()) {
        state.localSendTokens.delete(token);
        return res.status(400).json({ error: 'Message cannot be empty.' });
      }
      sent = await state.client.sendMessage(chatId, body);
    }

    const messageId = serializedId(sent.id);
    if (messageId) state.localMessageIds.set(messageId, Date.now() + 30000);
    setTimeout(() => state.localSendTokens.delete(token), 5000).unref();

    return res.status(201).json({
      ok: true,
      messageId,
      timestamp: sent.timestamp,
    });
  } catch (error) {
    state.localSendTokens.delete(token);
    return res.status(502).json({ error: error.message || String(error) });
  }
});

app.delete('/sessions/:sessionId', async (req, res) => {
  try {
    await logoutSession(req.params.sessionId);
    return res.json({ ok: true, status: 'disconnected' });
  } catch (error) {
    return res.status(500).json({ error: error.message });
  }
});

async function shutdown() {
  for (const [sessionId, state] of sessions.entries()) {
    try { await state.client.destroy(); } catch (_) {}
    await releaseLock(sessionId).catch(() => {});
  }
  if (redis) await redis.quit().catch(() => {});
  process.exit(0);
}
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);

(async () => {
  try {
    await startRedis();
    await restoreSessions();
    app.listen(PORT, '0.0.0.0', () => {
      console.log(`SHVYA WhatsApp Web gateway listening on ${PORT}`);
    });
  } catch (error) {
    console.error('Gateway startup failed:', error);
    process.exit(1);
  }
})();
