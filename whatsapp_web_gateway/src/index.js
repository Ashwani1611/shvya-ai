'use strict';

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
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
const HISTORY_CHAT_LIMIT = 50;
const HISTORY_MESSAGE_LIMIT = 20;
const HISTORY_CHAT_FETCH_TIMEOUT_MS = 12000;
const HISTORY_GET_CHATS_TIMEOUT_MS = 20000;
const EXISTING_CHATS_TIMEOUT_MS = 60000;

fs.mkdirSync(AUTH_PATH, { recursive: true });

const sessions = new Map();
let redis = null;

function digits(value) {
  return String(value || '').replace(/\D/g, '');
}

function serializedWid(value) {
  if (!value) return '';
  if (typeof value === 'string') return value;
  if (value._serialized) return String(value._serialized);
  if (value.$1) return String(value.$1);
  if (value.user && value.server) return `${value.user}@${value.server}`;
  return '';
}

function phoneNumberFromWid(value) {
  const serialized = serializedWid(value);
  if (!serialized.endsWith('@c.us')) return '';
  const phoneDigits = digits(serialized);
  if (phoneDigits.length < 8 || phoneDigits.length > 15) return '';
  return `+${phoneDigits}`;
}

function publicSession(sessionId, state) {
  return {
    sessionId,
    status: state.status,
    phoneNumber: state.phoneNumber || state.requestedPhone || '',
    lastError: state.lastError || '',
    historySyncing: Boolean(state.historySyncPromise),
    historySynced: Boolean(state.historySynced),
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

async function serializeMessage(message, chat = null, includeContactLookup = true) {
  const resolvedChat = chat || await message.getChat();
  let contactName = (resolvedChat && resolvedChat.name) || '';
  let contactPhoneNumber = phoneNumberFromWid(
    message.fromMe ? message.to : message.from,
  );

  if (includeContactLookup && !resolvedChat.isGroup) {
    try {
      const contact = await message.getContact();
      contactName = (
        contact && (contact.pushname || contact.name || contact.shortName)
      ) || contactName;
      contactPhoneNumber = (
        phoneNumberFromWid(contact && contact.id)
        || phoneNumberFromWid(contact && contact.phoneNumber)
        || contactPhoneNumber
      );
    } catch (_) {}
  }

  return {
    messageId: message.id && message.id._serialized,
    from: message.from,
    to: message.to,
    fromMe: Boolean(message.fromMe),
    body: message.body || '',
    messageType: mapMessageType(message.type),
    timestamp: message.timestamp,
    status: ackStatus(message.ack),
    chatId: resolvedChat && resolvedChat.id && resolvedChat.id._serialized,
    chatName: resolvedChat && resolvedChat.name,
    isGroup: Boolean(resolvedChat && resolvedChat.isGroup),
    contactName,
    contactPhoneNumber,
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

async function syncRecentHistory(sessionId, state) {
  const chats = await withTimeout(
    state.client.getChats(),
    HISTORY_GET_CHATS_TIMEOUT_MS,
    'getChats',
  );
  const selectedChats = chats.slice(0, HISTORY_CHAT_LIMIT);
  let syncedMessages = 0;
  let syncedChats = 0;

  for (const chat of selectedChats) {
    let messages;
    try {
      messages = await withTimeout(
        chat.fetchMessages({ limit: HISTORY_MESSAGE_LIMIT }),
        HISTORY_CHAT_FETCH_TIMEOUT_MS,
        `fetchMessages ${chat.id && chat.id._serialized}`,
      );
    } catch (error) {
      console.warn(
        `Could not fetch history for ${sessionId}/${chat.id && chat.id._serialized}:`,
        error.message,
      );
      continue;
    }

    const batch = [];
    for (const message of messages) {
      if (!message || !message.id || !message.id._serialized) continue;
      try {
        batch.push(await serializeMessage(message, chat, false));
      } catch (error) {
        console.warn(`Could not serialize history for ${sessionId}:`, error.message);
      }
    }

    if (batch.length) {
      await sendHistoryBatch(sessionId, batch);
      syncedMessages += batch.length;
    }
    syncedChats += 1;
  }

  return {
    chats: syncedChats,
    messages: syncedMessages,
  };
}

async function listExistingDirectChats(state) {
  const chats = await withTimeout(
    state.client.getChats(),
    EXISTING_CHATS_TIMEOUT_MS,
    'getChats for existing-chat snapshot',
  );
  const rows = [];
  let unresolved = 0;

  for (const chat of chats) {
    if (!chat || chat.isGroup) continue;

    const chatId = serializedWid(chat.id);
    if (!chatId || chatId.endsWith('@g.us') || chatId.endsWith('@broadcast')) {
      continue;
    }

    let contact = null;
    let phoneNumber = phoneNumberFromWid(chatId);

    if (!phoneNumber && chatId.endsWith('@lid')) {
      try {
        contact = await withTimeout(
          state.client.getContactById(chatId),
          5000,
          `resolve contact ${chatId}`,
        );
        phoneNumber = (
          phoneNumberFromWid(contact && contact.id)
          || phoneNumberFromWid(contact && contact.phoneNumber)
        );
      } catch (error) {
        console.warn(`Could not resolve LID chat ${chatId}:`, error.message);
      }
    }

    if (!phoneNumber) {
      if (chatId.endsWith('@lid')) unresolved += 1;
      continue;
    }

    const contactName = (
      (contact && (contact.pushname || contact.name || contact.shortName))
      || chat.name
      || phoneNumber
    );
    rows.push({
      chatId,
      phoneNumber,
      contactName,
      isGroup: false,
    });
  }

  return { chats: rows, unresolved };
}

function startHistorySync(sessionId, state, { force = false } = {}) {
  if (state.historySyncPromise) return state.historySyncPromise;
  if (state.historySynced && !force) {
    return Promise.resolve(state.historyResult || { chats: 0, messages: 0 });
  }

  state.historySyncPromise = syncRecentHistory(sessionId, state)
    .then((result) => {
      state.historySynced = true;
      state.historyResult = result;
      console.log(
        `Synced hosted history for ${sessionId}: ${result.chats} chats, ${result.messages} messages`,
      );
      return result;
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

    // The session is usable as soon as WhatsApp reports CONNECTED/ready.
    // History backfill must never block this transition because a slow chat
    // can otherwise leave the UI on QR/Syncing and Django on Pending forever.
    state.status = 'running';
    state.qr = null;
    state.qrGeneratedAt = 0;
    state.lastError = '';
    await callback(sessionId, 'ready', {
      phoneNumber: state.phoneNumber,
      source,
    });

    startHistorySync(sessionId, state).catch((error) => {
      state.historySynced = false;
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
  } catch (_) {
    // During Chromium startup getState can throw. The normal ready event or
    // the next status/QR poll will reconcile it once WhatsApp is available.
  }
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

  client.on('message', async (message) => {
    if (message.fromMe) return;
    try {
      await callback(
        sessionId,
        'message',
        await serializeMessage(message, null, true),
      );
    } catch (error) {
      console.warn(`Could not forward message for ${sessionId}:`, error.message);
    }
  });

  client.on('message_ack', async (message, ack) => {
    if (!message.fromMe) return;
    const status = ackStatus(ack);
    if (!status) return;
    await callback(sessionId, 'message_ack', {
      messageId: message.id && message.id._serialized,
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
    readyPromise: null,
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
  // Keep LocalAuth files. For an unpaired session this produces a fresh QR;
  // for an already-paired session it restores and returns to Running.
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
  const ageSeconds = state.qrGeneratedAt ? Math.floor((Date.now() - state.qrGeneratedAt) / 1000) : 0;
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
  if (state.status !== 'running') return res.status(409).json({ error: 'Session is not running.' });

  const to = String(req.body.to || '').trim();
  const body = String(req.body.body || '');
  if (!to) return res.status(400).json({ error: 'Recipient is required.' });
  const chatId = to.includes('@') ? to : `${digits(to)}@c.us`;
  if (!chatId || chatId === '@c.us') return res.status(400).json({ error: 'Invalid recipient.' });

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
      if (!body.trim()) return res.status(400).json({ error: 'Message cannot be empty.' });
      sent = await state.client.sendMessage(chatId, body);
    }
    return res.status(201).json({
      ok: true,
      messageId: sent.id && sent.id._serialized,
      timestamp: sent.timestamp,
    });
  } catch (error) {
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