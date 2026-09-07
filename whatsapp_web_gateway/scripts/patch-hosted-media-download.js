'use strict';

const fs = require('fs');
const path = require('path');

const target = path.join(process.cwd(), 'src', 'index.js');
let source = fs.readFileSync(target, 'utf8');

function lines(...items) {
  return items.join('\n');
}

function replaceOnce(before, after, label) {
  const first = source.indexOf(before);
  if (first === -1) {
    if (source.includes(after)) {
      console.log(`Hosted media download patch already applied: ${label}`);
      return;
    }
    throw new Error(`Unable to apply Hosted media download patch: ${label}`);
  }
  if (source.indexOf(before, first + before.length) !== -1) {
    throw new Error(`Hosted media download signature is not unique: ${label}`);
  }
  source = source.replace(before, after);
  console.log(`Applied Hosted media download patch: ${label}`);
}

const sendRoute = "app.post('/sessions/:sessionId/messages', async (req, res) => {";

replaceOnce(
  sendRoute,
  lines(
    "app.post('/sessions/:sessionId/media', async (req, res) => {",
    '  const sessionId = req.params.sessionId;',
    '  const state = sessions.get(sessionId);',
    "  if (!state) return res.status(404).json({ error: 'Session not found.' });",
    '  await reconcileClientState(sessionId, state);',
    "  if (state.status !== 'running') {",
    "    return res.status(409).json({ error: 'Session is not running.' });",
    '  }',
    "  const messageId = String(req.body.messageId || '').trim();",
    "  if (!messageId) return res.status(400).json({ error: 'messageId is required.' });",
    "  if (typeof state.client.getMessageById !== 'function') {",
    "    return res.status(501).json({ error: 'Media lookup is unavailable.' });",
    '  }',
    '  try {',
    '    const message = await withTimeout(',
    '      state.client.getMessageById(messageId),',
    '      10000,',
    "      'lookup media message ' + messageId,",
    '    );',
    "    if (!message) return res.status(404).json({ error: 'Message not found.' });",
    "    if (!message.hasMedia || typeof message.downloadMedia !== 'function') {",
    "      return res.status(404).json({ error: 'Message has no downloadable media.' });",
    '    }',
    '    const media = await withTimeout(',
    '      message.downloadMedia(),',
    '      30000,',
    "      'download media message ' + messageId,",
    '    );',
    "    if (!media || !media.data) return res.status(404).json({ error: 'Media is unavailable.' });",
    '    return res.json({',
    '      ok: true,',
    "      mimetype: String(media.mimetype || 'application/octet-stream'),",
    "      filename: String(media.filename || ''),",
    '      filesize: Number(media.filesize || 0),',
    '      data: media.data,',
    '    });',
    '  } catch (error) {',
    "    console.warn('Could not download Hosted media ' + messageId + ':', error.message);",
    "    return res.status(502).json({ error: error.message || 'Could not download media.' });",
    '  }',
    '});',
    '',
    sendRoute,
  ),
  'add authenticated media lookup endpoint',
);

fs.writeFileSync(target, source);

for (const marker of [
  "app.post('/sessions/:sessionId/media'",
  'state.client.getMessageById(messageId)',
  'message.downloadMedia()',
]) {
  if (!source.includes(marker)) {
    throw new Error(`Hosted media download verification failed: ${marker}`);
  }
}

console.log('Hosted media download runtime patch complete.');
