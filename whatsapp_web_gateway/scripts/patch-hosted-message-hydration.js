'use strict';

const fs = require('fs');
const path = require('path');

const target = path.join(process.cwd(), 'src', 'index.js');
let source = fs.readFileSync(target, 'utf8');

function replaceOnce(before, after, label) {
  const first = source.indexOf(before);
  if (first === -1) {
    if (source.includes(after)) {
      console.log(`Hosted message hydration patch already applied: ${label}`);
      return;
    }
    throw new Error(`Unable to apply Hosted message hydration patch: ${label}`);
  }
  if (source.indexOf(before, first + before.length) !== -1) {
    throw new Error(`Hosted message hydration signature is not unique: ${label}`);
  }
  source = source.replace(before, after);
  console.log(`Applied Hosted message hydration patch: ${label}`);
}

replaceOnce(
  `function mapMessageType(type) {\n  if (type === 'image') return 'image';\n  if (type === 'audio' || type === 'ptt') return 'audio';\n  if (type === 'video') return 'video';\n  if (type === 'document') return 'document';\n  return 'text';\n}\n`,
  `function mapMessageType(type) {\n  if (type === 'image') return 'image';\n  if (type === 'audio' || type === 'ptt') return 'audio';\n  if (type === 'video') return 'video';\n  if (type === 'document') return 'document';\n  return 'text';\n}\n\nfunction messageBodyCandidate(message) {\n  if (!message) return '';\n  const data = message._data && typeof message._data === 'object'\n    ? message._data\n    : {};\n  const listResponse = data.listResponse && data.listResponse.singleSelectReply;\n  const candidates = [\n    message.body,\n    data.body,\n    data.caption,\n    data.pollName,\n    data.eventName,\n    message.selectedButtonId,\n    message.selectedRowId,\n    data.selectedButtonId,\n    listResponse && listResponse.selectedRowId,\n  ];\n  for (const value of candidates) {\n    if (value === undefined || value === null) continue;\n    const text = String(value);\n    if (text.trim()) return text;\n  }\n  return '';\n}\n\nasync function resolveMessageBody(message, client) {\n  const body = messageBodyCandidate(message);\n  if (body) return body;\n\n  const rawType = String((message && message.type) || '').toLowerCase();\n  if (!['chat', 'text'].includes(rawType)) return '';\n  if (!client || typeof client.getMessageById !== 'function') return '';\n\n  const messageId = serializedId(message && message.id);\n  if (!messageId) return '';\n  try {\n    const refreshed = await withTimeout(\n      client.getMessageById(messageId),\n      5000,\n      \\`rehydrate message \\${messageId}\\`,\n    );\n    return messageBodyCandidate(refreshed);\n  } catch (error) {\n    console.warn(\\`Could not rehydrate empty Hosted text \\${messageId}:\\`, error.message);\n    return '';\n  }\n}\n`,
  'add empty text body rehydration helper',
);

replaceOnce(
  `    body: message.body || '',\n    messageType: mapMessageType(message.type),`,
  `    body: await resolveMessageBody(message, client),\n    messageType: mapMessageType(message.type),\n    rawMessageType: String(message.type || ''),\n    hasMedia: Boolean(message.hasMedia),\n    isStatus: Boolean(message.isStatus),\n    isEphemeral: Boolean(message.isEphemeral),`,
  'preserve raw WhatsApp type and hydration metadata',
);

fs.writeFileSync(target, source);

for (const marker of [
  'async function resolveMessageBody',
  'rawMessageType: String(message.type || \'\')',
  'rehydrate message',
]) {
  if (!source.includes(marker)) {
    throw new Error(`Hosted message hydration verification failed: ${marker}`);
  }
}

console.log('Hosted message hydration runtime patch complete.');
