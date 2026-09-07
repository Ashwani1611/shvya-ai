'use strict';

const fs = require('fs');
const path = require('path');

const target = path.join(process.cwd(), 'src', 'index.js');
let source = fs.readFileSync(target, 'utf8');

function replaceOnce(before, after, label) {
  const first = source.indexOf(before);
  if (first === -1) {
    if (source.includes(after)) {
      console.log(`Hosted chat runtime patch already applied: ${label}`);
      return;
    }
    throw new Error(`Unable to apply Hosted chat runtime patch: ${label}`);
  }
  if (source.indexOf(before, first + before.length) !== -1) {
    throw new Error(`Hosted chat patch signature is not unique: ${label}`);
  }
  source = source.replace(before, after);
  console.log(`Applied Hosted chat runtime patch: ${label}`);
}

replaceOnce(
  `const HISTORY_CHAT_LIMIT = 100;\nconst HISTORY_MESSAGE_LIMIT = 30;\nconst HISTORY_SYNC_CONCURRENCY = 4;\nconst HISTORY_CHAT_FETCH_TIMEOUT_MS = 12000;\nconst HISTORY_GET_CHATS_TIMEOUT_MS = 20000;\nconst EXISTING_CHATS_TIMEOUT_MS = 60000;\nconst CONTACT_LOOKUP_TIMEOUT_MS = 5000;\nconst LID_RESOLVE_BATCH_SIZE = 20;\nconst LID_RESOLVE_TIMEOUT_MS = 10000;\nconst LOCAL_SEND_MATCH_MS = 20000;`,
  `const HISTORY_DEEP_CHAT_LIMIT = 100;\nconst HISTORY_CHAT_INDEX_LIMIT = 1000;\nconst HISTORY_MESSAGE_LIMIT = 100;\nconst HISTORY_INDEX_MESSAGE_LIMIT = 1;\nconst HISTORY_SYNC_CONCURRENCY = 6;\nconst HISTORY_CHAT_FETCH_TIMEOUT_MS = 15000;\nconst HISTORY_GET_CHATS_TIMEOUT_MS = 30000;\nconst EXISTING_CHATS_TIMEOUT_MS = 60000;\nconst CONTACT_LOOKUP_TIMEOUT_MS = 5000;\nconst LID_RESOLVE_BATCH_SIZE = 50;\nconst LID_RESOLVE_TIMEOUT_MS = 10000;\nconst LOCAL_SEND_MATCH_MS = 20000;`,
  'expanded history index and LID resolver limits',
);

replaceOnce(
  `async function callback(sessionId, event, payload = {}) {\n  if (!CALLBACK_URL || !CALLBACK_TOKEN) return false;\n  try {\n    const response = await fetch(CALLBACK_URL, {\n      method: 'POST',\n      headers: {\n        'Content-Type': 'application/json',\n        'X-SHVYA-Hosted-Token': CALLBACK_TOKEN,\n      },\n      body: JSON.stringify({ sessionId, event, ...payload }),\n      signal: AbortSignal.timeout(10000),\n    });\n    if (!response.ok) {\n      console.warn(\`Hosted callback \${event} for \${sessionId} returned \${response.status}\`);\n      return false;\n    }\n    return true;\n  } catch (error) {\n    console.warn(\`Hosted callback \${event} for \${sessionId} failed:\`, error.message);\n    return false;\n  }\n}\n`,
  `async function callback(sessionId, event, payload = {}) {\n  if (!CALLBACK_URL || !CALLBACK_TOKEN) return false;\n  let lastError = '';\n  for (let attempt = 1; attempt <= 3; attempt += 1) {\n    try {\n      const response = await fetch(CALLBACK_URL, {\n        method: 'POST',\n        headers: {\n          'Content-Type': 'application/json',\n          'X-SHVYA-Hosted-Token': CALLBACK_TOKEN,\n        },\n        body: JSON.stringify({ sessionId, event, ...payload }),\n        signal: AbortSignal.timeout(event === 'history_sync' ? 30000 : 10000),\n      });\n      if (response.ok) return true;\n      lastError = \`HTTP \${response.status}\`;\n      if (response.status >= 400 && response.status < 500) break;\n    } catch (error) {\n      lastError = error.message || String(error);\n    }\n    if (attempt < 3) {\n      await new Promise((resolve) => setTimeout(resolve, attempt * 250));\n    }\n  }\n  console.warn(\`Hosted callback \${event} for \${sessionId} failed after retries: \${lastError}\`);\n  return false;\n}\n`,
  'retry transient gateway callbacks',
);

replaceOnce(
  `async function syncOneChat(sessionId, chat, client) {\n  const rawChatId = serializedId(chat && chat.id);`,
  `async function syncOneChat(\n  sessionId,\n  chat,\n  client,\n  { messageLimit = HISTORY_MESSAGE_LIMIT, lidPhoneMap = null } = {},\n) {\n  const rawChatId = serializedId(chat && chat.id);`,
  'parameterize per-chat history depth',
);

replaceOnce(
  `    identity = await resolveChatIdentity(chat, client);`,
  `    identity = await resolveChatIdentity(chat, client, lidPhoneMap);`,
  'reuse batch LID identity during history sync',
);

replaceOnce(
  `      chat.fetchMessages({ limit: HISTORY_MESSAGE_LIMIT }),`,
  `      chat.fetchMessages({ limit: messageLimit }),`,
  'fetch requested history depth',
);

replaceOnce(
  `  const selectedChats = chats.slice(0, HISTORY_CHAT_LIMIT);\n  let cursor = 0;\n  let syncedMessages = 0;\n  let syncedChats = 0;\n\n  async function worker() {\n    while (true) {\n      const index = cursor++;\n      if (index >= selectedChats.length) return;\n      const result = await syncOneChat(\n        sessionId,\n        selectedChats[index],\n        state.client,\n      );\n      syncedChats += result.chats;\n      syncedMessages += result.messages;\n    }\n  }`,
  `  const selectedChats = chats.slice(0, HISTORY_CHAT_INDEX_LIMIT);\n  const lidPhoneMap = await resolveLidPhoneMap(\n    state.client,\n    selectedChats.map((chat) => serializedId(chat && chat.id)),\n  );\n  let cursor = 0;\n  let syncedMessages = 0;\n  let syncedChats = 0;\n\n  async function worker() {\n    while (true) {\n      const index = cursor++;\n      if (index >= selectedChats.length) return;\n      const messageLimit = index < HISTORY_DEEP_CHAT_LIMIT\n        ? HISTORY_MESSAGE_LIMIT\n        : HISTORY_INDEX_MESSAGE_LIMIT;\n      const result = await syncOneChat(\n        sessionId,\n        selectedChats[index],\n        state.client,\n        { messageLimit, lidPhoneMap },\n      );\n      syncedChats += result.chats;\n      syncedMessages += result.messages;\n    }\n  }`,
  'index older chats while deeply syncing recent conversations',
);

replaceOnce(
  `function isGatewayOriginatedOwnMessage(state, message) {\n  pruneLocalSendState(state);\n  const messageId = serializedId(message && message.id);\n  if (messageId && state.localMessageIds.has(messageId)) return true;\n\n  const to = serializedId(message && message.to) || String((message && message.to) || '');\n  const body = String((message && message.body) || '');\n  for (const token of state.localSendTokens) {\n    if (!sameRecipient(token.chatId, to)) continue;\n    if (!token.media && token.body !== body) continue;\n    if (messageId) state.localMessageIds.set(messageId, Date.now() + 30000);\n    token.matched = true;\n    return true;\n  }\n  return false;\n}\n`,
  `function isGatewayOriginatedOwnMessage(state, message) {\n  pruneLocalSendState(state);\n  const messageId = serializedId(message && message.id);\n  if (messageId && state.localMessageIds.has(messageId)) return true;\n\n  const to = serializedId(message && message.to) || String((message && message.to) || '');\n  const body = String((message && message.body) || '');\n  const bodyMatches = [];\n  for (const token of state.localSendTokens) {\n    if (!token.media && token.body !== body) continue;\n    bodyMatches.push(token);\n    if (!sameRecipient(token.chatId, to)) continue;\n    if (messageId) state.localMessageIds.set(messageId, Date.now() + 30000);\n    token.matched = true;\n    return true;\n  }\n\n  // WhatsApp can emit a newly-sent direct message with an @lid recipient even\n  // when SHVYA sent it to the corresponding @c.us id. If exactly one recent\n  // send has the same body/media, treat it as the same outbound message.\n  if (to.endsWith('@lid') && bodyMatches.length === 1) {\n    const token = bodyMatches[0];\n    if (messageId) state.localMessageIds.set(messageId, Date.now() + 30000);\n    token.matched = true;\n    return true;\n  }\n  return false;\n}\n`,
  'suppress outbound echoes when WhatsApp rewrites c.us to LID',
);

fs.writeFileSync(target, source);

for (const marker of [
  'HISTORY_CHAT_INDEX_LIMIT = 1000',
  'HISTORY_INDEX_MESSAGE_LIMIT = 1',
  'resolveLidPhoneMap',
  'failed after retries',
  'bodyMatches.length === 1',
]) {
  if (!source.includes(marker)) {
    throw new Error(`Hosted chat runtime patch verification failed: ${marker}`);
  }
}

console.log('Hosted chat complete-sync runtime patch complete.');
