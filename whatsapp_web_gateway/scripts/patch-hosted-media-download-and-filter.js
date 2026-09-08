'use strict';

const fs = require('fs');
const path = require('path');

const target = path.join(process.cwd(), 'src', 'index.js');
let source = fs.readFileSync(target, 'utf8');

function replaceOnce(before, after, label) {
  const first = source.indexOf(before);
  if (first === -1) {
    if (source.includes(after)) {
      console.log(`Hosted media/filter patch already applied: ${label}`);
      return;
    }
    throw new Error(`Unable to apply Hosted media/filter patch: ${label}`);
  }
  if (source.indexOf(before, first + before.length) !== -1) {
    throw new Error(`Hosted media/filter signature is not unique: ${label}`);
  }
  source = source.replace(before, after);
  console.log(`Applied Hosted media/filter patch: ${label}`);
}

replaceOnce(
  "        unsafeMime: false,",
  "        unsafeMime: true,",
  'allow authenticated follow-up media URLs without file extensions',
);

replaceOnce(
  "      sent = await state.client.sendMessage(chatId, media, {\n        caption: body || undefined,\n      });",
  "      sent = await state.client.sendMessage(chatId, media, {\n        caption: body || undefined,\n        sendMediaAsDocument: String(req.body.messageType || '').toLowerCase() === 'document',\n      });",
  'preserve document send semantics for URL-backed follow-ups',
);

const wireMarker = 'function wireClientEvents(sessionId, state) {';
if (!source.includes('function isHiddenHostedChatId(value) {')) {
  if (!source.includes(wireMarker)) {
    throw new Error('Unable to find Hosted client event insertion point.');
  }
  const helper = `function isHiddenHostedChatId(value) {
  const chatId = serializedId(value) || String(value || '');
  return Boolean(
    chatId && (
      chatId === 'status@broadcast' ||
      chatId.includes('@newsletter')
    )
  );
}

function isHiddenHostedMessage(message) {
  if (!message) return false;
  if (message.isStatus) return true;
  const data = message._data && typeof message._data === 'object' ? message._data : {};
  const ids = [
    message.from,
    message.to,
    data.from,
    data.to,
    data.chatId,
    data.remote,
    data.id && data.id.remote,
  ];
  return ids.some((value) => isHiddenHostedChatId(value));
}

`;
  source = source.replace(wireMarker, helper + wireMarker);
}

replaceOnce(
  "    if (message.fromMe) return;",
  "    if (message.fromMe || isHiddenHostedMessage(message)) return;",
  'drop inbound WhatsApp Status/newsletter callbacks',
);

replaceOnce(
  "    if (!message.fromMe || isGatewayOriginatedOwnMessage(state, message)) return;",
  "    if (!message.fromMe || isHiddenHostedMessage(message) || isGatewayOriginatedOwnMessage(state, message)) return;",
  'drop linked-device WhatsApp Status/newsletter callbacks',
);

replaceOnce(
  "  if (!rawChatId || rawChatId === 'status@broadcast' || rawChatId.includes('@newsletter')) {",
  "  if (!rawChatId || isHiddenHostedChatId(rawChatId)) {",
  'exclude WhatsApp Status/newsletter chats from history sync',
);

const messagesRoute = "app.post('/sessions/:sessionId/messages', async (req, res) => {";
if (!source.includes("'/sessions/:sessionId/messages/:messageId/media'")) {
  if (!source.includes(messagesRoute)) {
    throw new Error('Unable to find Hosted messages route insertion point.');
  }

  const mediaRoute = `app.get('/sessions/:sessionId/messages/:messageId/media', async (req, res) => {
  const state = sessions.get(req.params.sessionId);
  if (!state) return res.status(404).json({ error: 'Session not found.' });
  await reconcileClientState(req.params.sessionId, state);
  if (state.status !== 'running') {
    return res.status(409).json({ error: 'Session is not running.' });
  }

  const messageId = String(req.params.messageId || '').trim();
  if (!messageId) return res.status(400).json({ error: 'Message id is required.' });

  try {
    const message = await withTimeout(
      state.client.getMessageById(messageId),
      15000,
      'getMessageById ' + messageId,
    );
    if (!message || !message.hasMedia) {
      return res.status(404).json({ error: 'Message media is not available.' });
    }

    const media = await withTimeout(
      message.downloadMedia(),
      90000,
      'downloadMedia ' + messageId,
    );
    if (!media || !media.data) {
      return res.status(410).json({ error: 'WhatsApp media has expired or cannot be downloaded.' });
    }

    const data = Buffer.from(String(media.data), 'base64');
    const contentType = String(media.mimetype || 'application/octet-stream')
      .split(';', 1)[0]
      .trim() || 'application/octet-stream';
    const filename = String(media.filename || ('whatsapp-' + messageId))
      .replace(/[\\r\\n]/g, '')
      .slice(0, 240);

    res.set('Content-Type', contentType);
    res.set('Content-Length', String(data.length));
    res.set('Cache-Control', 'private, max-age=60');
    res.set('X-SHVYA-Filename-B64', Buffer.from(filename, 'utf8').toString('base64url'));
    return res.send(data);
  } catch (error) {
    return res.status(502).json({ error: error.message || String(error) });
  }
});

`;
  source = source.replace(messagesRoute, mediaRoute + messagesRoute);
}

fs.writeFileSync(target, source);

for (const expected of [
  'unsafeMime: true',
  'function isHiddenHostedChatId',
  'isHiddenHostedMessage(message)',
  "'/sessions/:sessionId/messages/:messageId/media'",
  'message.downloadMedia()',
  'sendMediaAsDocument',
]) {
  if (!source.includes(expected)) {
    throw new Error(`Hosted media/filter verification failed: ${expected}`);
  }
}

console.log('Hosted media download, follow-up MIME, and Status filter patch complete.');
