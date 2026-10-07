// Narrow operations-group routes. Mounted behind the gateway's existing auth.
const GROUP_ID = /^[0-9]+(?:-[0-9]+)?@g\.us$/;

function exactGroupId(value) {
  if (typeof value !== 'string' || value.length > 80 || !GROUP_ID.test(value)) {
    throw Object.assign(new Error('An exact WhatsApp group ID is required.'), { statusCode: 400 });
  }
  return value;
}

function boundedLimit(value) {
  const limit = value === undefined ? 50 : Number(value);
  if (!Number.isInteger(limit) || limit < 1 || limit > 100) {
    throw Object.assign(new Error('Limit must be an integer from 1 to 100.'), { statusCode: 400 });
  }
  return limit;
}

function groupMember(chat, state, serializedId) {
  const ownId = serializedId(state.client.info && state.client.info.wid);
  return Boolean(chat.isGroup && ownId && Array.isArray(chat.participants)
    && chat.participants.some(item => serializedId(item.id) === ownId));
}

function registerGroupOperations(app, { sessions, reconcileClientState, serializedId, withTimeout, idempotentSend, redis, journalRoot }) {
  async function running(sessionId) {
    const state = sessions.get(sessionId);
    if (!state) throw Object.assign(new Error('Session not found.'), { statusCode: 404 });
    await reconcileClientState(sessionId, state);
    if (state.status !== 'running') throw Object.assign(new Error('Session is not running.'), { statusCode: 409 });
    return state;
  }

  async function exactChat(state, groupId) {
    const chat = await withTimeout(state.client.getChatById(groupId), 20000, 'Read operations group');
    if (serializedId(chat.id) !== groupId || !groupMember(chat, state, serializedId)) {
      throw Object.assign(new Error('The session is not a member of this exact group.'), { statusCode: 403 });
    }
    return chat;
  }

  app.get('/sessions/:sessionId/groups', async (req, res) => {
    try {
      const limit = boundedLimit(req.query.limit);
      const state = await running(req.params.sessionId);
      const chats = await withTimeout(state.client.getChats(), 20000, 'List operations groups');
      const groups = chats.filter(chat => GROUP_ID.test(serializedId(chat.id)) && groupMember(chat, state, serializedId));
      return res.json({ groups: groups.slice(0, limit).map(chat => ({
        id: serializedId(chat.id), name: String(chat.name || '').slice(0, 500),
        participantCount: chat.participants.length,
      })), total: groups.length, truncated: groups.length > limit });
    } catch (error) { return res.status(error.statusCode || 502).json({ error: error.message }); }
  });

  app.get('/sessions/:sessionId/groups/:groupId/messages', async (req, res) => {
    try {
      const groupId = exactGroupId(req.params.groupId);
      const limit = boundedLimit(req.query.limit);
      const state = await running(req.params.sessionId);
      const chat = await exactChat(state, groupId);
      const messages = await withTimeout(chat.fetchMessages({ limit }), 20000, 'Read operations group messages');
      return res.json({ groupId, name: String(chat.name || '').slice(0, 500), messages: messages.slice(-limit).map(item => ({
        id: serializedId(item.id), author: String(item.author || ''), fromMe: Boolean(item.fromMe),
        body: String(item.body || '').slice(0, 10000), type: String(item.type || ''),
        timestamp: item.timestamp, hasMedia: Boolean(item.hasMedia),
      })) });
    } catch (error) { return res.status(error.statusCode || 502).json({ error: error.message }); }
  });

  app.post('/sessions/:sessionId/groups/:groupId/messages', async (req, res) => {
    try {
      const groupId = exactGroupId(req.params.groupId);
      const body = req.body.body;
      if (typeof body !== 'string' || !body.trim() || body.length > 10500) {
        return res.status(400).json({ error: 'Text message must contain 1–10500 characters.' });
      }
      const requestId = req.body.requestId;
      if (typeof requestId !== 'string' || !/^[a-f0-9-]{36}$/i.test(requestId)) {
        return res.status(400).json({ error: 'Approval receipt request ID is required.' });
      }
      let state;
      const outcome = await idempotentSend({
        redis, journalRoot, sessionId: req.params.sessionId, requestId,
        payload: ['operations-group', groupId, body],
        prepare: async () => {
          state = await running(req.params.sessionId);
          await exactChat(state, groupId);
        },
        send: async () => {
          const sent = await state.client.sendMessage(groupId, body);
          return { ok: true, groupId, messageId: serializedId(sent.id), timestamp: sent.timestamp };
        },
      });
      if (outcome.retryAfter) res.set('Retry-After', String(outcome.retryAfter));
      return res.status(outcome.status).json(outcome.body);
    } catch (error) { return res.status(error.statusCode || 502).json({ error: error.message }); }
  });
}

module.exports = { registerGroupOperations, exactGroupId, boundedLimit, groupMember };
