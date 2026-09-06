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

fs.mkdirSync(AUTH_PATH, { recursive: true });

const sessions = new Map();
let redis = null;

function digits(value) {
  return String(value || '').replace(/\D/g, '');
}

function publicSession(sessionId, state) {
  return {
    sessionId,
    status: state.status,
    phoneNumber: state.phoneNumber || state.requestedPhone || '',
    lastError: state.lastError || '',
  };
}

async function callback(sessionId, event, payload = {}) {
  if (!CALLBACK_URL || !CALLBACK_TOKEN) return;
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
    }
  } catch (error) {
    console.warn(`Hosted callback ${event} for ${sessionId} failed:`, error.message);
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
    await callback(sessionId, 'authenticated');
  });

  client.on('auth_failure', async (message) => {
    state.status = 'failed';
    state.lastError = String(message || 'Authentication failed');
    await callback(sessionId, 'auth_failure', { error: state.lastError });
  });

  client.on('ready', async () => {
    state.status = 'syncing';
    state.phoneNumber = `+${digits(client.info && client.info.wid && client.info.wid.user)}`;
    await callback(sessionId, 'syncing', { phoneNumber: state.phoneNumber });

    const requested = digits(state.requestedPhone);
    const connected = digits(state.phoneNumber);
    if (requested && connected && requested !== connected) {
      state.status = 'failed';
      state.lastError = 'Scanned WhatsApp number does not match the pipeline-linked number.';
      await callback(sessionId, 'failed', {
        phoneNumber: state.phoneNumber,
        error: state.lastError,
      });
      try { await client.logout(); } catch (_) {}
      return;
    }

    try {
      // A lightweight initial chat fetch gives the linked device a chance to
      // finish its first sync before SHVYA exposes the session as Running.
      await client.getChats();
    } catch (_) {}

    state.status = 'running';
    state.lastError = '';
    await callback(sessionId, 'ready', { phoneNumber: state.phoneNumber });
  });

  client.on('message', async (message) => {
    if (message.fromMe) return;
    try {
      const chat = await message.getChat();
      const contact = await message.getContact();
      await callback(sessionId, 'message', {
        messageId: message.id && message.id._serialized,
        from: message.from,
        to: message.to,
        body: message.body || '',
        messageType: mapMessageType(message.type),
        timestamp: message.timestamp,
        chatId: chat && chat.id && chat.id._serialized,
        chatName: chat && chat.name,
        isGroup: Boolean(chat && chat.isGroup),
        contactName: (contact && (contact.pushname || contact.name || contact.shortName)) || '',
      });
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
    state.lastError = String(reason || 'Disconnected');
    await callback(sessionId, 'disconnected', { reason: state.lastError });
  });
}

async function createSession(sessionId, requestedPhone = '') {
  if (!/^[-_\w]+$/i.test(sessionId)) throw new Error('Invalid session id.');

  const existing = sessions.get(sessionId);
  if (existing) {
    if (requestedPhone) existing.requestedPhone = requestedPhone;
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
    state.lastError = error.message || String(error);
    await callback(sessionId, 'failed', { error: state.lastError });
  });

  return state;
}

async function refreshQr(sessionId) {
  const current = sessions.get(sessionId);
  if (!current) return createSession(sessionId);
  if (current.status === 'running') return current;

  const requestedPhone = current.requestedPhone;
  try { await current.client.destroy(); } catch (_) {}
  sessions.delete(sessionId);
  // Keep LocalAuth files. For an unpaired session this simply starts another
  // browser and produces a fresh QR; for a valid paired session it restores.
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

app.get('/sessions/:sessionId', (req, res) => {
  const state = sessions.get(req.params.sessionId);
  if (!state) return res.status(404).json({ error: 'Session not found.' });
  return res.json(publicSession(req.params.sessionId, state));
});

app.get('/sessions/:sessionId/qr', (req, res) => {
  const state = sessions.get(req.params.sessionId);
  if (!state) return res.status(404).json({ error: 'Session not found.' });
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

app.post('/sessions/:sessionId/messages', async (req, res) => {
  const state = sessions.get(req.params.sessionId);
  if (!state) return res.status(404).json({ error: 'Session not found.' });
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
