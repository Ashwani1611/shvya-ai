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

const mapType = lines(
  'function mapMessageType(type) {',
  "  if (type === 'image') return 'image';",
  "  if (type === 'audio' || type === 'ptt') return 'audio';",
  "  if (type === 'video') return 'video';",
  "  if (type === 'document') return 'document';",
  "  return 'text';",
  '}',
);

replaceOnce(
  mapType,
  lines(
    mapType,
    '',
    'function messageBodyCandidate(message) {',
    "  if (!message) return '';",
    "  const data = message._data && typeof message._data === 'object' ? message._data : {};",
    '  const listResponse = data.listResponse && data.listResponse.singleSelectReply;',
    '  const candidates = [',
    '    message.body,',
    '    data.body,',
    '    data.caption,',
    '    data.pollName,',
    '    data.eventName,',
    '    message.selectedButtonId,',
    '    message.selectedRowId,',
    '    data.selectedButtonId,',
    '    listResponse && listResponse.selectedRowId,',
    '  ];',
    '  for (const value of candidates) {',
    '    if (value === undefined || value === null) continue;',
    '    const text = String(value);',
    '    if (text.trim()) return text;',
    '  }',
    "  return '';",
    '}',
    '',
    'async function resolveMessageBody(message, client) {',
    '  const body = messageBodyCandidate(message);',
    '  if (body) return body;',
    "  const rawType = String((message && message.type) || '').toLowerCase();",
    "  if (!['chat', 'text'].includes(rawType)) return '';",
    "  if (!client || typeof client.getMessageById !== 'function') return '';",
    '  const messageId = serializedId(message && message.id);',
    "  if (!messageId) return '';",
    '  try {',
    '    const refreshed = await withTimeout(',
    '      client.getMessageById(messageId),',
    '      5000,',
    "      'rehydrate message ' + messageId,",
    '    );',
    '    return messageBodyCandidate(refreshed);',
    '  } catch (error) {',
    "    console.warn('Could not rehydrate empty Hosted text ' + messageId + ':', error.message);",
    "    return '';",
    '  }',
    '}',
  ),
  'add empty text body rehydration helper',
);

replaceOnce(
  lines(
    "    body: message.body || '',",
    '    messageType: mapMessageType(message.type),',
  ),
  lines(
    '    body: await resolveMessageBody(message, client),',
    '    messageType: mapMessageType(message.type),',
    "    rawMessageType: String(message.type || ''),",
    '    hasMedia: Boolean(message.hasMedia),',
    '    isStatus: Boolean(message.isStatus),',
    '    isEphemeral: Boolean(message.isEphemeral),',
  ),
  'preserve raw WhatsApp type and hydration metadata',
);

fs.writeFileSync(target, source);

for (const marker of [
  'async function resolveMessageBody',
  "rawMessageType: String(message.type || '')",
  'rehydrate message ',
]) {
  if (!source.includes(marker)) {
    throw new Error(`Hosted message hydration verification failed: ${marker}`);
  }
}

console.log('Hosted message hydration runtime patch complete.');
