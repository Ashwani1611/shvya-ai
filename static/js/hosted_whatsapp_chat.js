(function () {
  'use strict';

  const app = document.getElementById('hosted-chat-app');
  if (!app) return;

  const dataUrl = app.dataset.dataUrl;
  const sendUrl = app.dataset.sendUrl;
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
    lastRefresh: Date.now(),
    threadLoaded: Boolean(initial.get('chat')),
  };

  let searchTimer = null;
  let refreshTimer = null;
  let socket = null;
  let reconnectTimer = null;
  let reconnectDelay = 1200;

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

  function setLive(online) {
    if (!liveStatus) return;
    liveStatus.classList.toggle('is-offline', !online);
    const label = liveStatus.querySelector('[data-live-label]');
    if (label) label.textContent = online ? 'Live' : 'Reconnecting';
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
    const unread = row.unread
      ? `<span class="hosted-unread">${Number(row.unread) || 0}</span>`
      : '';
    return `<div class="hosted-conversation-inner"><div class="hosted-conversation-avatar">${esc(initial)}</div><div class="hosted-conversation-content"><div class="hosted-conversation-top"><div class="hosted-conversation-name">${esc(name)}</div><div class="hosted-conversation-time">${esc(fmtList(row.last_at))}</div></div>${phone}<div class="flex items-center justify-between gap-2"><div class="hosted-conversation-preview">${esc(conversationPreview(row))}</div>${unread}</div></div></div>`;
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
      const signature = JSON.stringify([row.name, row.phone, row.last_message, row.last_at, row.unread, state.selected === key]);
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
      message.status, message.created_at, message.media_url, message.media_download_url, message.filename
    ]);
  }

  function updateBubble(node, message) {
    const outbound = message.direction === 'outbound';
    const signature = bubbleSignature(message);
    if (node.dataset.signature === signature) return;
    node.className = `bubble ${outbound ? 'out' : 'in'}`;
    node.innerHTML = `${messageContent(message)}<span class="bubble-time">${esc(fmtBubble(message.created_at))}${statusHtml(message)}</span>`;
    node.dataset.signature = signature;
    node.dataset.mediaEnhanced = '1';
  }

  function renderThread(data) {
    state.selected = data.selected_chat || state.selected || '';
    const hasChat = Boolean(state.selected);
    app.classList.toggle('has-chat', hasChat);
    header?.classList.toggle('hidden', !hasChat);
    thread?.classList.toggle('hidden', !hasChat);
    form?.classList.toggle('hidden', !hasChat);
    empty?.classList.toggle('hidden', hasChat);
    if (!hasChat) return;

    const selectedName = data.selected_name || state.selected;
    if (threadName && threadName.textContent !== selectedName) threadName.textContent = selectedName;
    if (threadKey && threadKey.textContent !== state.selected) threadKey.textContent = state.selected;
    if (threadAvatar) threadAvatar.textContent = selectedName.trim().charAt(0).toUpperCase() || '?';
    if (chatInput) chatInput.value = state.selected;

    const stick = nearBottom();
    const savedTop = thread.scrollTop;
    const messages = data.thread || [];

    if (!messages.length) {
      const optimistic = thread.querySelector('[data-optimistic="1"]');
      if (!optimistic && thread.dataset.empty !== '1') {
        thread.innerHTML = '<div class="thread-no-messages"><i class="ti ti-message-circle text-xl"></i><div class="mt-2 text-sm font-semibold">No messages yet</div><div class="mt-1 text-xs">New messages appear here live.</div></div>';
        thread.dataset.empty = '1';
      }
      return;
    }

    delete thread.dataset.empty;
    thread.querySelectorAll('.thread-no-messages').forEach(node => node.remove());
    const oldHeight = thread.scrollHeight;
    const existing = new Map();
    thread.querySelectorAll('.bubble[data-message-id]').forEach(node => existing.set(node.dataset.messageId, node));

    let cursor = thread.firstElementChild;
    messages.forEach(message => {
      const id = String(message.id || '');
      if (!id) return;
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
    });

    existing.forEach(node => {
      if (node.dataset.optimistic !== '1') node.remove();
    });

    if (stick || !state.threadLoaded) {
      thread.scrollTop = thread.scrollHeight;
      state.threadLoaded = true;
    } else {
      const delta = thread.scrollHeight - oldHeight;
      thread.scrollTop = savedTop + Math.min(0, delta);
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

  async function refresh({ updateHistory = false } = {}) {
    if (state.busy) {
      state.pending = true;
      return;
    }
    state.busy = true;
    const seq = ++state.seq;
    const params = new URLSearchParams();
    if (state.selected) params.set('chat', state.selected);
    if (state.query) params.set('q', state.query);

    try {
      const response = await fetch(`${dataUrl}?${params.toString()}`, {
        headers: { 'X-Requested-With': 'XMLHttpRequest' },
        cache: 'no-store'
      });
      if (!response.ok) throw new Error('Could not refresh hosted chats');
      const data = await response.json();
      if (seq !== state.seq) return;
      renderList(data);
      renderThread(data);
      state.lastRefresh = Date.now();
      if (updateHistory) updateUrl();
    } catch (error) {
      console.warn(error.message);
    } finally {
      if (seq === state.seq) state.busy = false;
      if (state.pending) {
        state.pending = false;
        scheduleRefresh(70);
      }
    }
  }

  list?.addEventListener('click', event => {
    const link = event.target.closest('[data-chat-key]');
    if (!link) return;
    event.preventDefault();
    const next = link.dataset.chatKey || '';
    if (state.selected === next) return;
    state.selected = next;
    state.threadLoaded = false;
    updateUrl();
    refresh();
  });

  search?.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.query = search.value.trim();
      state.selected = '';
      state.threadLoaded = false;
      app.classList.remove('has-chat');
      updateUrl();
      refresh();
    }, 220);
  });

  mobileBack?.addEventListener('click', () => app.classList.remove('has-chat'));

  form?.addEventListener('submit', async event => {
    event.preventDefault();
    const body = form.elements.body.value.trim();
    if (!body || !state.selected) return;
    const button = form.querySelector('button[type=submit]');
    button.disabled = true;
    form.elements.body.value = '';
    const optimistic = appendOptimistic(body);

    try {
      const response = await fetch(sendUrl, {
        method: 'POST',
        headers: { 'X-CSRFToken': csrf, 'Content-Type': 'application/json' },
        body: JSON.stringify({ chat: state.selected, body })
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Could not send message');
      const realId = String(result.message?.id || '');
      if (optimistic && realId) {
        optimistic.dataset.messageId = realId;
        optimistic.removeAttribute('data-optimistic');
        optimistic.classList.remove('optimistic');
      }
      scheduleRefresh(55);
    } catch (error) {
      if (optimistic) optimistic.remove();
      form.elements.body.value = body;
      alert(error.message);
      scheduleRefresh(100);
    } finally {
      button.disabled = false;
      form.elements.body.focus();
    }
  });

  function scheduleRefresh(delay = 80) {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => refresh(), delay);
  }

  function connectSocket() {
    clearTimeout(reconnectTimer);
    if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;
    try {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      socket = new WebSocket(`${proto}://${location.host}/ws/whatsapp/hosted/${accountId}/`);
      socket.onopen = () => {
        reconnectDelay = 1200;
        setLive(true);
      };
      socket.onmessage = event => {
        try {
          const data = JSON.parse(event.data);
          if (data.kind === 'refresh') scheduleRefresh(data.reason === 'status' ? 110 : 45);
        } catch (_) {}
      };
      socket.onclose = () => {
        socket = null;
        setLive(false);
        reconnectTimer = setTimeout(connectSocket, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 1.6, 6000);
      };
      socket.onerror = () => { try { socket.close(); } catch (_) {} };
    } catch (_) {
      socket = null;
      setLive(false);
      reconnectTimer = setTimeout(connectSocket, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 1.6, 6000);
    }
  }

  connectSocket();

  // WebSocket is primary. Polling is only a quiet consistency fallback.
  setInterval(() => {
    if (document.visibilityState !== 'visible' || state.busy) return;
    const healthy = socket && socket.readyState === WebSocket.OPEN;
    const staleFor = Date.now() - state.lastRefresh;
    if ((healthy && staleFor > 30000) || (!healthy && staleFor > 4500)) refresh();
  }, 2200);

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      connectSocket();
      if (Date.now() - state.lastRefresh > 4000) scheduleRefresh(100);
    }
  });
  window.addEventListener('online', () => { connectSocket(); scheduleRefresh(100); });
  window.addEventListener('beforeunload', () => {
    clearTimeout(reconnectTimer);
    clearTimeout(refreshTimer);
    if (socket) socket.close();
  });

  if (thread && !thread.classList.contains('hidden')) {
    thread.scrollTop = thread.scrollHeight;
    state.threadLoaded = true;
  }
})();
