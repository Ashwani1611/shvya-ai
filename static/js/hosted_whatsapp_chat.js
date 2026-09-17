(function () {
  'use strict';

  const app = document.getElementById('hosted-chat-app');
  if (!app) return;

  const dataUrl = app.dataset.dataUrl;
  const sendUrl = app.dataset.sendUrl;
  const readUrl = app.dataset.readUrl;
  const accountId = app.dataset.accountId;
  const list = document.getElementById('conversation-list');
  const search = document.getElementById('chat-search');
  const searchMeta = document.getElementById('search-meta');
  const header = document.getElementById('thread-header');
  const thread = document.getElementById('thread-scroll');
  const empty = document.getElementById('thread-empty');
  const threadName = document.getElementById('thread-name');
  const threadKey = document.getElementById('thread-key');
  const threadAvatar = document.getElementById('thread-avatar');
  const form = document.getElementById('chat-form');
  const chatInput = document.getElementById('chat-key-input');
  const mobileBack = document.getElementById('mobile-chat-back');
  const liveStatus = document.getElementById('hosted-live-status');
  const csrf = document.querySelector('input[name=csrfmiddlewaretoken]')?.value || '';
  const initial = new URLSearchParams(location.search);

  const state = {
    selected: initial.get('chat') || '',
    query: initial.get('q') || search?.value || '',
    seq: 0,
    busy: false,
    pending: false,
    lastRefresh: 0,
    accountStatus: app.dataset.accountStatus || 'connected',
    messages: new Map(),
    nextBefore: '',
    olderLoaded: false,
    olderBusy: false,
    readSignature: '',
    readBusy: false,
    stopped: false,
    unavailable: false,
    socketSeen: 0,
    stickToBottom: true,
    threadLoaded: Boolean(initial.get('chat')),
  };

  let searchTimer = null;
  let refreshTimer = null;
  let socket = null;
  let reconnectTimer = null;
  let reconnectDelay = 1200;
  let controller = null;
  let olderController = null;

  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[char]));

  const fmtList = iso => {
    if (!iso) return '';
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return '';
    const now = new Date();
    const sameDay = now.toDateString() === date.toDateString();
    return new Intl.DateTimeFormat(undefined, sameDay
      ? { hour: 'numeric', minute: '2-digit' }
      : { month: 'short', day: 'numeric' }).format(date);
  };

  const fmtBubble = iso => {
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? '' : new Intl.DateTimeFormat(undefined, {
      hour: 'numeric', minute: '2-digit'
    }).format(date);
  };

  function setLive() {
    if (!liveStatus) return;
    let label = 'Connecting';
    let online = false;
    if (state.unavailable) label = 'Account unavailable';
    else if (navigator.onLine === false) label = 'Offline';
    else if (state.accountStatus === 'disconnected' || state.accountStatus === 'failed') label = 'WhatsApp disconnected';
    else if (state.accountStatus !== 'connected') label = 'Connecting WhatsApp';
    else if (socket && socket.readyState === WebSocket.OPEN && Date.now() - state.socketSeen < 65000) {
      label = 'Live'; online = true;
    } else if (state.lastRefresh && Date.now() - state.lastRefresh < 30000) {
      label = 'Connected · polling'; online = true;
    } else if (state.lastRefresh) label = 'Reconnecting';
    liveStatus.classList.toggle('is-offline', !online);
    const text = liveStatus.querySelector('[data-live-label]');
    if (text) text.textContent = label;
  }

  function updateUrl() {
    const params = new URLSearchParams();
    if (state.selected) params.set('chat', state.selected);
    if (state.query) params.set('q', state.query);
    history.replaceState(null, '', location.pathname + (params.toString() ? '?' + params.toString() : ''));
  }

  function nearBottom() {
    return !thread || thread.scrollHeight - thread.scrollTop - thread.clientHeight < 150;
  }

  function looksTechnicalBody(value, messageType) {
    const raw = String(value || '').trim();
    if (!raw || messageType === 'text') return false;
    const lower = raw.toLowerCase();
    if (lower.startsWith('data:') || lower.startsWith('blob:') || lower.includes('base64,')) return true;
    if (raw.length > 550 && (raw.startsWith('{') || raw.startsWith('['))) return true;
    if (raw.length > 260 && !/\s/.test(raw)) return true;
    if (raw.length > 700) {
      const sample = raw.replace(/\s/g, '').slice(0, 500);
      if (/^[a-z0-9+/=_-]+$/i.test(sample)) return true;
    }
    return false;
  }

  function safeCaption(message) {
    const body = String(message.body || '').trim();
    return looksTechnicalBody(body, String(message.message_type || 'text')) ? '' : body;
  }

  function statusHtml(message) {
    if (message.direction !== 'outbound') return '';
    const status = String(message.status || '').toLowerCase();
    if (status === 'failed') return '<span class="bubble-status" title="Failed">!</span>';
    if (status === 'queued') return '<span class="bubble-status" title="Queued">◷</span>';
    if (status === 'delivered') return '<span class="bubble-status" title="Delivered">✓✓</span>';
    if (status === 'read') return '<span class="bubble-status read" title="Read">✓✓</span>';
    return '<span class="bubble-status" title="Sent">✓</span>';
  }

  function captionHtml(message) {
    const caption = safeCaption(message);
    return caption ? `<div class="bubble-body" dir="auto">${esc(caption)}</div>` : '';
  }

  function messageContent(message) {
    const type = String(message.message_type || 'text').toLowerCase();
    const mediaUrl = String(message.media_url || '');
    const downloadUrl = String(message.media_download_url || mediaUrl);
    const filename = String(message.filename || message.message_type_label || 'Attachment');

    if (type === 'image' && mediaUrl) {
      return `<div class="hosted-media-wrap"><a href="${esc(mediaUrl)}" target="_blank" rel="noopener"><img class="hosted-media-image" src="${esc(mediaUrl)}" alt="Shared image" loading="lazy"></a></div>${captionHtml(message)}<a class="hosted-media-download" href="${esc(downloadUrl)}"><i class="ti ti-download"></i><span>Download</span></a>`;
    }
    if (type === 'video' && mediaUrl) {
      return `<div class="hosted-media-wrap"><video class="hosted-media-video" controls preload="metadata" src="${esc(mediaUrl)}"></video></div>${captionHtml(message)}<a class="hosted-media-download" href="${esc(downloadUrl)}"><i class="ti ti-download"></i><span>Download</span></a>`;
    }
    if (type === 'audio' && mediaUrl) {
      return `<audio class="hosted-media-audio" controls preload="metadata" src="${esc(mediaUrl)}"></audio>${captionHtml(message)}`;
    }
    if (type === 'document' && downloadUrl) {
      return `<a class="hosted-document-card" href="${esc(downloadUrl)}"><i class="ti ti-file-description"></i><span>${esc(filename)}</span><i class="ti ti-download"></i></a>${captionHtml(message)}`;
    }
    if (type !== 'text') {
      return `<div class="bubble-media-label"><i class="ti ti-paperclip"></i>${esc(message.message_type_label || filename)}</div>${captionHtml(message)}`;
    }

    const body = String(message.body || '').trim();
    return body
      ? `<div class="bubble-body" dir="auto">${esc(body)}</div>`
      : '<div class="bubble-body"><span class="bubble-placeholder">Message has no text content</span></div>';
  }

  function conversationPreview(row) {
    const preview = String(row.last_message || '').trim();
    if (!preview) return '';
    if (looksTechnicalBody(preview, 'media')) return 'Attachment';
    return preview.length > 180 ? preview.slice(0, 177) + '…' : preview;
  }

  function conversationMarkup(row) {
    const name = String(row.name || row.phone || row.key || 'Chat');
    const initial = name.trim().charAt(0).toUpperCase() || '?';
    const phone = row.phone && row.phone !== row.name
      ? `<div class="hosted-conversation-phone">${esc(row.phone)}</div>`
      : '';
    const stage = row.stage_name
      ? `<span class="hosted-stage" title="Lead stage: ${esc(row.stage_name)}">${esc(row.stage_name)}</span>` : '';
    const unread = row.unread
      ? `<span class="hosted-unread">${Number(row.unread) || 0}</span>`
      : '';
    return `<div class="hosted-conversation-inner"><div class="hosted-conversation-avatar">${esc(initial)}</div><div class="hosted-conversation-content"><div class="hosted-conversation-top"><div class="hosted-conversation-name">${esc(name)}</div><div class="hosted-conversation-time">${esc(fmtList(row.last_at))}</div></div>${phone}<div class="flex items-center justify-between gap-2"><div class="hosted-conversation-preview">${esc(conversationPreview(row))}</div>${unread}</div>${stage}</div></div>`;
  }

  function renderList(data) {
    const rows = data.conversations || [];
    const savedTop = list.scrollTop;
    searchMeta?.classList.toggle('hidden', !state.query);
    if (searchMeta) searchMeta.textContent = state.query ? `Search results for “${state.query}”` : '';

    if (!rows.length) {
      if (list.dataset.empty !== '1') {
        list.innerHTML = `<div class="hosted-empty"><div><i class="ti ti-message-circle-off text-4xl"></i><p class="mt-3 text-sm">${state.query ? 'No matching chats' : 'No chats yet'}</p></div></div>`;
        list.dataset.empty = '1';
      }
      list.scrollTop = 0;
      return;
    }

    delete list.dataset.empty;
    const existing = new Map();
    list.querySelectorAll('.hosted-conversation[data-chat-key]').forEach(node => existing.set(node.dataset.chatKey, node));
    list.querySelectorAll('.hosted-empty').forEach(node => node.remove());

    let cursor = list.firstElementChild;
    rows.forEach(row => {
      const key = String(row.key || '');
      if (!key) return;
      let node = existing.get(key);
      if (!node) {
        node = document.createElement('a');
        node.className = 'hosted-conversation';
        node.dataset.chatKey = key;
      }
      const signature = JSON.stringify([row.name, row.phone, row.last_message, row.last_at, row.unread, row.stage_name, row.lead_id, state.selected === key]);
      if (node.dataset.signature !== signature) {
        node.classList.toggle('active', state.selected === key);
        node.href = `?chat=${encodeURIComponent(key)}${state.query ? '&q=' + encodeURIComponent(state.query) : ''}`;
        node.innerHTML = conversationMarkup(row);
        node.dataset.signature = signature;
      }
      if (node !== cursor) list.insertBefore(node, cursor);
      cursor = node.nextElementSibling;
      existing.delete(key);
    });
    existing.forEach(node => node.remove());
    list.scrollTop = savedTop;
  }

  function bubbleSignature(message) {
    return JSON.stringify([
      message.direction, message.body, message.message_type, message.message_type_label,
      message.created_at, message.media_url, message.media_download_url, message.filename
    ]);
  }

  function updateBubble(node, message) {
    const outbound = message.direction === 'outbound';
    const signature = bubbleSignature(message);
    if (node.dataset.signature !== signature) {
      node.className = `bubble ${outbound ? 'out' : 'in'}`;
      node.innerHTML = `${messageContent(message)}<span class="bubble-time"></span>`;
      node.dataset.signature = signature;
      node.dataset.mediaEnhanced = '1';
    }
    // A delivery tick must not recreate an image, interrupt audio/video, or
    // change the scroll anchor. Only the small timestamp/status node changes.
    const time = node.querySelector('.bubble-time');
    const timeHtml = `${esc(fmtBubble(message.created_at))}${statusHtml(message)}`;
    if (time && time.innerHTML !== timeHtml) time.innerHTML = timeHtml;
    node.dataset.createdAt = message.created_at || '';
  }

  function showThread(hasChat) {
    app.classList.toggle('has-chat', hasChat);
    header?.classList.toggle('hidden', !hasChat);
    thread?.classList.toggle('hidden', !hasChat);
    form?.classList.toggle('hidden', !hasChat);
    empty?.classList.toggle('hidden', hasChat);
    if (form?.elements.body) form.elements.body.disabled = !hasChat;
  }

  function renderThread(data, { older = false } = {}) {
    state.selected = data.selected_chat || '';
    const hasChat = Boolean(state.selected);
    showThread(hasChat);
    if (!hasChat) return;
    const selectedName = data.selected_name || state.selected;
    if (threadName) threadName.textContent = selectedName;
    if (threadKey) threadKey.textContent = state.selected;
    if (threadAvatar) threadAvatar.textContent = selectedName.trim().charAt(0).toUpperCase() || '?';
    if (chatInput) chatInput.value = state.selected;
    const stick = !older && (nearBottom() || !state.threadLoaded);
    const savedTop = thread.scrollTop;
    const oldHeight = thread.scrollHeight;
    for (const message of data.thread || []) {
      if (message.id) state.messages.set(String(message.id), message);
    }
    const messages = Array.from(state.messages.values()).sort((a, b) =>
      String(a.created_at).localeCompare(String(b.created_at)) || String(a.id).localeCompare(String(b.id))
    );
    if (older || !state.olderLoaded) state.nextBefore = data.next_before || '';
    if (older) state.olderLoaded = true;
    thread.querySelectorAll('.thread-no-messages,.thread-loading,.thread-error,.hosted-load-older').forEach(node => node.remove());
    const existing = new Map();
    thread.querySelectorAll('.bubble[data-message-id]').forEach(node => existing.set(node.dataset.messageId, node));
    let cursor = thread.firstElementChild;
    for (const message of messages) {
      const id = String(message.id);
      let node = existing.get(id);
      if (!node) {
        node = document.createElement('div');
        node.dataset.messageId = id;
      }
      node.removeAttribute('data-optimistic');
      node.classList.remove('optimistic');
      updateBubble(node, message);
      if (node !== cursor) thread.insertBefore(node, cursor);
      cursor = node.nextElementSibling;
      existing.delete(id);
    }
    // Preserve only local sends not yet returned by the server. Older loaded
    // messages live in the keyed map instead of disappearing on each refresh.
    existing.forEach(node => {
      if (node.dataset.optimistic !== '1') node.remove();
    });
    if (state.nextBefore) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'hosted-load-older';
      button.textContent = state.olderBusy ? 'Loading earlier messages…' : 'Load earlier messages';
      button.disabled = state.olderBusy;
      button.addEventListener('click', loadOlder);
      thread.prepend(button);
    }
    if (!messages.length && !thread.querySelector('[data-optimistic="1"]')) {
      thread.innerHTML = '<div class="thread-no-messages">No messages yet. New messages appear here live.</div>';
    }
    if (older) thread.scrollTop = savedTop + (thread.scrollHeight - oldHeight);
    else if (stick) thread.scrollTop = thread.scrollHeight;
    else thread.scrollTop = savedTop;
    state.threadLoaded = true;
    state.stickToBottom = stick;
    if (!older && stick) acknowledgeRead(data);
  }

  async function acknowledgeRead(data) {
    if (!readUrl || !data.read_token || document.visibilityState !== 'visible' || state.readBusy) return;
    const row = (data.conversations || []).find(item => item.key === state.selected);
    if (!row || !Number(row.unread)) return;
    const last = (data.thread || []).at(-1);
    const signature = `${state.selected}:${last?.id || ''}:${row.unread}`;
    if (state.readSignature === signature) return;
    state.readBusy = true;
    const readController = new AbortController();
    const timeout = setTimeout(() => readController.abort(), 12000);
    try {
      const response = await fetch(readUrl, {
        method: 'POST', headers: { 'X-CSRFToken': csrf, 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: data.read_token }), signal: readController.signal,
      });
      if (!response.ok) throw new Error('Read receipt could not be saved');
      state.readSignature = signature;
      scheduleRefresh(100);
    } catch (_) {
      // Leave the real badge unchanged; the next successful snapshot retries.
    } finally {
      clearTimeout(timeout);
      state.readBusy = false;
    }
  }

  function appendOptimistic(body) {
    if (!state.selected) return null;
    thread.querySelector('.thread-no-messages')?.remove();
    delete thread.dataset.empty;
    const node = document.createElement('div');
    const tempId = `optimistic-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    node.className = 'bubble out optimistic';
    node.dataset.messageId = tempId;
    node.dataset.optimistic = '1';
    node.innerHTML = `<div class="bubble-body" dir="auto">${esc(body)}</div><span class="bubble-time">${esc(fmtBubble(new Date().toISOString()))}<span class="bubble-status" title="Queued">◷</span></span>`;
    thread.appendChild(node);
    thread.scrollTop = thread.scrollHeight;
    return node;
  }

  async function refresh({ force = false } = {}) {
    if (state.stopped || state.unavailable) return;
    if (force) {
      controller?.abort();
      state.seq += 1;
      state.busy = false;
      state.pending = false;
    }
    if (state.busy) { state.pending = true; return; }
    state.busy = true;
    const seq = ++state.seq;
    const selected = state.selected;
    const query = state.query;
    const params = new URLSearchParams();
    if (selected) params.set('chat', selected);
    if (query) params.set('q', query);
    const request = new AbortController();
    controller = request;
    const timeout = setTimeout(() => request.abort(), 12000);
    try {
      const response = await fetch(`${dataUrl}?${params}`, {
        headers: { 'X-Requested-With': 'XMLHttpRequest' }, cache: 'no-store', signal: request.signal,
      });
      if ([401, 403, 404].includes(response.status)) state.unavailable = true;
      if (!response.ok) throw new Error('Could not refresh chats. Please retry.');
      const data = await response.json();
      if (seq !== state.seq || selected !== state.selected || query !== state.query || state.stopped) return;
      state.accountStatus = data.account_status || state.accountStatus;
      renderThread(data);
      renderList(data);
      state.lastRefresh = Date.now();
      updateUrl();
    } catch (error) {
      if (seq !== state.seq || state.stopped) return;
      if (selected && !state.threadLoaded) {
        thread.innerHTML = '<div class="thread-error" role="status">Messages could not load. <button type="button" data-chat-retry>Retry</button></div>';
      }
    } finally {
      clearTimeout(timeout);
      if (seq === state.seq) {
        state.busy = false;
        controller = null;
        setLive();
        if (state.pending) { state.pending = false; scheduleRefresh(100); }
      }
    }
  }

  function selectChat(next, name = '') {
    olderController?.abort();
    state.olderBusy = false;
    state.selected = next;
    state.threadLoaded = false;
    state.messages.clear();
    state.nextBefore = '';
    state.olderLoaded = false;
    state.readSignature = '';
    state.stickToBottom = true;
    thread.innerHTML = next ? '<div class="thread-loading" role="status">Loading messages…</div>' : '';
    showThread(Boolean(next));
    if (threadName) threadName.textContent = name || next;
    if (threadKey) threadKey.textContent = next;
    if (threadAvatar) threadAvatar.textContent = (name || next).trim().charAt(0).toUpperCase();
    if (chatInput) chatInput.value = next;
    list.querySelectorAll('[data-chat-key]').forEach(node => node.classList.toggle('active', node.dataset.chatKey === next));
    updateUrl();
    refresh({ force: true });
  }

  async function loadOlder() {
    if (!state.selected || !state.nextBefore || state.olderBusy || state.stopped) return;
    state.olderBusy = true;
    const selected = state.selected;
    const before = state.nextBefore;
    const params = new URLSearchParams({ chat: selected, before, q: state.query });
    const request = new AbortController();
    olderController = request;
    const timeout = setTimeout(() => request.abort(), 12000);
    const button = thread.querySelector('.hosted-load-older');
    if (button) { button.disabled = true; button.textContent = 'Loading earlier messages…'; }
    try {
      const response = await fetch(`${dataUrl}?${params}`, {
        headers: { 'X-Requested-With': 'XMLHttpRequest' }, cache: 'no-store', signal: request.signal,
      });
      if (!response.ok) throw new Error('Earlier messages could not load');
      const data = await response.json();
      if (selected !== state.selected || olderController !== request || state.stopped) return;
      state.olderBusy = false;
      renderThread(data, { older: true });
    } catch (_) {
      if (selected === state.selected && button) button.textContent = 'Retry loading earlier messages';
    } finally {
      clearTimeout(timeout);
      if (olderController === request) { state.olderBusy = false; olderController = null; }
      if (button) button.disabled = false;
    }
  }

  list?.addEventListener('click', event => {
    const link = event.target.closest('[data-chat-key]');
    if (!link) return;
    event.preventDefault();
    const next = link.dataset.chatKey || '';
    if (state.selected === next && state.threadLoaded) { showThread(true); return; }
    selectChat(next, link.querySelector('.hosted-conversation-name')?.textContent || next);
  });
  thread?.addEventListener('click', event => {
    if (event.target.closest('[data-chat-retry]')) refresh({ force: true });
  });
  thread?.addEventListener('scroll', () => {
    state.stickToBottom = nearBottom();
    if (thread.scrollTop < 70 && state.threadLoaded) loadOlder();
  }, { passive: true });
  const settleMedia = () => { if (state.stickToBottom) thread.scrollTop = thread.scrollHeight; };
  thread?.addEventListener('load', settleMedia, true);
  thread?.addEventListener('loadedmetadata', settleMedia, true);

  search?.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.query = search.value.trim();
      selectChat('');
    }, 220);
  });
  mobileBack?.addEventListener('click', () => app.classList.remove('has-chat'));

  form?.addEventListener('submit', async event => {
    event.preventDefault();
    const body = form.elements.body.value.trim();
    const chat = state.selected;
    if (!body || !chat || !state.threadLoaded || state.unavailable) return;
    const button = form.querySelector('button[type=submit]');
    if (button.disabled) return;
    button.disabled = true;
    form.elements.body.value = '';
    const optimistic = appendOptimistic(body);
    const request = new AbortController();
    const timeout = setTimeout(() => request.abort(), 20000);
    try {
      const response = await fetch(sendUrl, {
        method: 'POST', headers: { 'X-CSRFToken': csrf, 'Content-Type': 'application/json' },
        body: JSON.stringify({ chat, body }), signal: request.signal,
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Could not send message');
      const realId = String(result.message?.id || '');
      if (optimistic && realId) {
        optimistic.dataset.messageId = realId;
        // Retain the optimistic node until its persisted message arrives.
      }
      scheduleRefresh(55);
    } catch (error) {
      if (optimistic) optimistic.remove();
      if (state.selected === chat) form.elements.body.value = body;
      alert(error.name === 'AbortError' ? 'Delivery confirmation timed out. Check the conversation before sending again.' : error.message);
      scheduleRefresh(100);
    } finally {
      clearTimeout(timeout);
      button.disabled = false;
      if (state.selected === chat) form.elements.body.focus();
    }
  });

  function scheduleRefresh(delay = 80) {
    // Throttle rather than indefinitely postponing under a busy message stream.
    if (refreshTimer || state.stopped || state.unavailable) return;
    refreshTimer = setTimeout(() => { refreshTimer = null; refresh(); }, delay);
  }

  function reconnect() {
    if (state.stopped || state.unavailable || reconnectTimer) return;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null; connectSocket();
    }, reconnectDelay + Math.floor(Math.random() * 500));
    reconnectDelay = Math.min(reconnectDelay * 1.7, 30000);
  }

  function connectSocket() {
    if (state.stopped || state.unavailable || navigator.onLine === false) return;
    if (socket && [WebSocket.OPEN, WebSocket.CONNECTING].includes(socket.readyState)) return;
    clearTimeout(reconnectTimer); reconnectTimer = null;
    try {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      const current = new WebSocket(`${proto}://${location.host}/ws/whatsapp/hosted/${accountId}/`);
      socket = current;
      current.onopen = () => {
        if (socket !== current) return;
        reconnectDelay = 1200;
        state.socketSeen = Date.now();
        setLive(); scheduleRefresh(100);
      };
      current.onmessage = event => {
        if (socket !== current) return;
        state.socketSeen = Date.now();
        try {
          const data = JSON.parse(event.data);
          if (data.kind === 'refresh') scheduleRefresh(data.reason === 'status' ? 150 : 80);
        } catch (_) {}
      };
      current.onclose = event => {
        if (socket !== current) return;
        socket = null;
        if ([4001, 4003].includes(event.code)) state.unavailable = true;
        setLive(); reconnect();
      };
      current.onerror = () => { try { current.close(); } catch (_) {} };
    } catch (_) { socket = null; setLive(); reconnect(); }
  }

  connectSocket();
  const pollTimer = setInterval(() => {
    if (state.stopped || state.unavailable || document.visibilityState !== 'visible') return;
    if (socket?.readyState === WebSocket.OPEN && Date.now() - state.socketSeen > 65000) socket.close();
    const healthy = socket?.readyState === WebSocket.OPEN;
    if (!state.busy && Date.now() - state.lastRefresh > (healthy ? 15000 : 5000)) refresh();
    setLive();
  }, 2200);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') { connectSocket(); scheduleRefresh(100); }
  });
  window.addEventListener('online', () => { connectSocket(); scheduleRefresh(100); });
  window.addEventListener('offline', setLive);
  window.addEventListener('beforeunload', () => {
    state.stopped = true;
    clearInterval(pollTimer);
    clearTimeout(reconnectTimer); clearTimeout(refreshTimer); clearTimeout(searchTimer);
    controller?.abort(); olderController?.abort();
    if (socket) socket.close();
  });
  if (thread && !thread.classList.contains('hidden')) thread.scrollTop = thread.scrollHeight;
  refresh();
})();
