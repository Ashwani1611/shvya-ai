'use strict';

const fs = require('fs');
const path = require('path');

const target = path.join(process.cwd(), 'src', 'index.js');
let source = fs.readFileSync(target, 'utf8');

const marker = "app.post('/sessions/:sessionId/messages', async (req, res) => {";
if (!source.includes(marker)) {
  if (source.includes("'/sessions/:sessionId/uploaded-media'")) {
    console.log('Hosted uploaded-media send patch already applied.');
    process.exit(0);
  }
  throw new Error('Unable to find Hosted messages route insertion point.');
}

const route = `function decodeHostedHeader(value) {
  if (!value) return '';
  try {
    return Buffer.from(String(value), 'base64url').toString('utf8');
  } catch (_) {
    return '';
  }
}

app.post(
  '/sessions/:sessionId/uploaded-media',
  express.raw({ type: '*/*', limit: '25mb' }),
  async (req, res) => {
    const state = sessions.get(req.params.sessionId);
    if (!state) return res.status(404).json({ error: 'Session not found.' });
    await reconcileClientState(req.params.sessionId, state);
    if (state.status !== 'running') {
      return res.status(409).json({ error: 'Session is not running.' });
    }

    const to = String(req.get('X-SHVYA-To') || '').trim();
    const messageType = String(req.get('X-SHVYA-Media-Type') || '').trim().toLowerCase();
    const allowedTypes = new Set(['image', 'video', 'document', 'audio']);
    if (!to) return res.status(400).json({ error: 'Recipient is required.' });
    if (!allowedTypes.has(messageType)) {
      return res.status(400).json({ error: 'Unsupported media type.' });
    }

    const chatId = to.includes('@') ? to : \`${'${digits(to)}'}@c.us\`;
    if (!chatId || chatId === '@c.us') {
      return res.status(400).json({ error: 'Invalid recipient.' });
    }

    const contentType = String(req.get('Content-Type') || 'application/octet-stream')
      .split(';', 1)[0]
      .trim()
      .toLowerCase();
    if (messageType === 'image' && !contentType.startsWith('image/')) {
      return res.status(400).json({ error: 'Uploaded photo is not an image.' });
    }
    if (messageType === 'video' && !contentType.startsWith('video/')) {
      return res.status(400).json({ error: 'Uploaded video is not a video.' });
    }
    if (messageType === 'audio' && !contentType.startsWith('audio/')) {
      return res.status(400).json({ error: 'Uploaded audio is not audio.' });
    }

    const data = Buffer.isBuffer(req.body) ? req.body : Buffer.alloc(0);
    if (!data.length) return res.status(400).json({ error: 'Uploaded file is empty.' });

    const filename = decodeHostedHeader(req.get('X-SHVYA-Filename-B64')) || 'attachment';
    const caption = decodeHostedHeader(req.get('X-SHVYA-Caption-B64'));
    const token = {
      chatId,
      body: caption,
      media: true,
      startedAt: Date.now(),
      matched: false,
    };
    state.localSendTokens.add(token);

    try {
      const media = new MessageMedia(contentType, data.toString('base64'), filename);
      const sent = await state.client.sendMessage(chatId, media, {
        caption: caption || undefined,
        sendMediaAsDocument: messageType === 'document',
      });
      const messageId = serializedId(sent.id);
      if (messageId) state.localMessageIds.set(messageId, Date.now() + 30000);
      setTimeout(() => state.localSendTokens.delete(token), 5000).unref();
      return res.status(201).json({
        ok: true,
        messageId,
        timestamp: sent.timestamp,
        messageType,
      });
    } catch (error) {
      state.localSendTokens.delete(token);
      return res.status(502).json({ error: error.message || String(error) });
    }
  },
);

`;

source = source.replace(marker, route + marker);
fs.writeFileSync(target, source);

for (const expected of [
  "'/sessions/:sessionId/uploaded-media'",
  "express.raw({ type: '*/*', limit: '25mb' })",
  'sendMediaAsDocument',
]) {
  if (!source.includes(expected)) {
    throw new Error(`Hosted uploaded-media verification failed: ${expected}`);
  }
}

console.log('Hosted uploaded-media send runtime patch complete.');
