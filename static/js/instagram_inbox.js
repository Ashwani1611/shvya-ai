/* Instagram inbox: same authenticated routes, keyed DOM updates, no Meta I/O. */
(() => {
  'use strict';
  const initial = document.getElementById('instagram-initial');
  const shell = document.getElementById('instagram-inbox-shell');
  if (!initial || !shell) return;
  const boot = JSON.parse(initial.textContent);
  const byId = id => document.getElementById(id);
  const surface = shell.querySelector('.wa-chat-surface');
  const messages = byId('ig-messages');
  const list = byId('ig-conversations');
  const input = byId('message-body');
  const form = byId('composer-form');
  const submit = form.querySelector('[type=submit]');
  const search = byId('ig-search');
  const older = byId('ig-older');
  const more = byId('ig-more-chats');
  const drafts = new Map();
  const pending = new Map();
  let active = boot.active_conversation, nextOffset = boot.next_offset;
  let before = active?.before || '', failures = 0, timer, controller, generation = 0;
  let requestRunning = false, navigationRunning = false, sendRunning = false, stopped = false, searchTimer;
  let following = true;
  const nearBottom = () => surface.scrollHeight - surface.scrollTop - surface.clientHeight < 100;
  const bottom = () => { surface.scrollTop = surface.scrollHeight; };
  const uuid = () => crypto.randomUUID();

  function report(text) { byId('ig-live-state').textContent = text; }
  function sendError(text) {
    byId('ig-send-error').textContent = text;
    byId('ig-send-error').hidden = !text;
  }
  function htmlNode(html) {
    // HTML is rendered by autoescaped Django partials; never use provider JSON
    // as markup. Media URLs are projected/validated by the backend.
    const node = new DOMParser().parseFromString(html, 'text/html').body.firstElementChild;
    if (!node) throw new Error('Invalid inbox response.');
    return node;
  }
  function formatTimes(root) {
    root.querySelectorAll('time[data-time]').forEach(node => {
      const stamp = new Date(node.dataset.time);
      if (!Number.isNaN(stamp.getTime())) {
        node.textContent = stamp.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
        node.title = stamp.toLocaleString();
      }
    });
  }
  function bindMedia(root) {
    root.querySelectorAll('img,video,audio').forEach(media => {
      if (media.dataset.igBound) return;
      media.dataset.igBound = '1';
      media.addEventListener('error', () => {
        const fallback = media.closest('.ig-media')?.querySelector('.ig-media-fallback');
        if (fallback) fallback.hidden = false;
      });
      media.addEventListener('load', () => { if (following) bottom(); });
      media.addEventListener('loadedmetadata', () => { if (following) bottom(); });
    });
  }
  function upsertMessages(items, prepend = false) {
    const wasNear = following && nearBottom();
    const height = surface.scrollHeight, top = surface.scrollTop;
    const existing = new Map([...messages.children].map(node => [node.dataset.messageId, node]));
    const fragment = document.createDocumentFragment();
    for (const item of items) {
      const old = existing.get(item.id);
      if (!old) { fragment.append(htmlNode(item.html)); continue; }
      if (old.dataset.contentVersion !== item.content_version) {
        old.replaceWith(htmlNode(item.html));
      } else {
        // Receipt/queued->sent changes must not restart a playing media element.
        old.dataset.time = item.created_time;
        const stamp = old.querySelector('time[data-time]');
        if (stamp) stamp.dataset.time = item.created_time;
        const status = old.querySelector('[data-message-status]');
        if (status) status.textContent = item.status.charAt(0).toUpperCase() + item.status.slice(1);
        const error = old.querySelector('[data-message-error]');
        if (error) { error.textContent = item.error || ''; error.hidden = !item.error; }
      }
    }
    if (prepend) messages.prepend(fragment); else messages.append(fragment);
    // Insert delayed events at their chronological position. Leave already
    // ordered nodes untouched, preserving media playback and selection.
    const sorted = [...messages.children].sort((a, b) =>
      a.dataset.time.localeCompare(b.dataset.time) || a.dataset.messageId.localeCompare(b.dataset.messageId));
    sorted.forEach((node, index) => { if (messages.children[index] !== node) messages.insertBefore(node, messages.children[index] || null); });
    formatTimes(messages); bindMedia(messages);
    if (prepend) surface.scrollTop = top + surface.scrollHeight - height;
    else if (wasNear) bottom();
  }
  function renderList(data, append = false) {
    if (!append) list.replaceChildren();
    const ids = new Set([...list.querySelectorAll('[data-conversation-id]')].map(node => node.dataset.conversationId));
    for (const item of data.conversations) {
      if (ids.has(item.id)) continue;
      list.append(htmlNode(item.html));
    }
    if (!list.children.length) {
      const p = document.createElement('p'); p.className = 'p-6 text-sm text-[#667781]';
      p.textContent = 'No conversations found. Customer-started DMs appear here after Meta delivers them.';
      list.append(p);
    }
    nextOffset = data.next_offset;
    more.hidden = nextOffset == null;
    list.querySelectorAll('[data-conversation-id]').forEach(node => node.setAttribute('aria-current', String(node.dataset.conversationId === active?.id)));
    formatTimes(list);
  }
  function policy() {
    let allowed = !!active?.policy?.can_reply;
    let text = active?.policy?.reason || 'Select a customer-started conversation to reply.';
    const expiry = Date.parse(active?.policy?.expires_at || '');
    if (allowed && Number.isFinite(expiry)) {
      const remaining = expiry - Date.now();
      if (remaining <= 0) { allowed = false; text = 'The 24-hour reply window has closed. Wait for a new customer message.'; }
      else {
        const minutes = Math.ceil(remaining / 60000);
        text += ` ${Math.floor(minutes / 60)}h ${minutes % 60}m remaining.`;
      }
    }
    byId('ig-policy').textContent = text;
    input.disabled = !allowed;
    submit.disabled = !allowed || sendRunning;
  }
  function showActive(next, reset) {
    if (reset) {
      if (active) drafts.set(active.id, input.value);
      messages.replaceChildren();
    }
    active = next;
    const intelligence = byId('ig-lead-intelligence');
    if (intelligence) {
      intelligence.hidden = !active;
      byId('ig-lead-score').textContent = active?.intent_score?.assessed ? `AI ${active.intent_score.score}/10` : 'AI · Not assessed';
      byId('ig-link-lead').textContent = active?.lead_name || 'Link to a CRM lead';
      if (active?.lead_url) byId('ig-link-lead').setAttribute('href', active.lead_url);
      else byId('ig-link-lead').removeAttribute('href');
    }
    shell.classList.toggle('wa-has-active-chat', !!active);
    shell.classList.toggle('wa-empty-chat', !active);
    surface.id = active ? 'thread' : 'ig-thread';
    byId('ig-empty').hidden = !!active;
    byId('ig-chat-name').textContent = active?.participant_name || 'Instagram';
    byId('ig-details-name').textContent = active?.participant_name || 'Instagram inbox';
    byId('ig-chat-username').textContent = active ? '@' + active.participant_username : '';
    if (active) {
      form.action = active.send_url;
      if (reset) { input.value = drafts.get(active.id) || ''; before = active.before; following = true; }
      upsertMessages(active.messages);
      if (reset) bottom();
    } else {
      form.removeAttribute('action'); input.value = ''; before = '';
    }
    older.hidden = !before;
    policy();
  }
  function requestURL(path = active?.url || boot.list_url, extra = {}) {
    const url = new URL(path, location.origin);
    if (url.origin !== location.origin) throw new Error('Invalid inbox URL.');
    if (search.value.trim()) url.searchParams.set('q', search.value.trim());
    Object.entries(extra).forEach(([key, value]) => { if (value !== '' && value != null) url.searchParams.set(key, value); });
    return url;
  }
  async function jsonFetch(url, options = {}) {
    const response = await fetch(url, {credentials:'same-origin', cache:'no-store', ...options,
      headers:{Accept:'application/json', ...options.headers}});
    if (response.redirected || !response.headers.get('content-type')?.includes('application/json')) {
      throw new Error('Your session may have expired. Reload and sign in again.');
    }
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Instagram updates are temporarily unavailable.');
    return data;
  }
  function schedule() {
    clearTimeout(timer);
    if (!stopped) timer = setTimeout(poll, Math.min(30000, 4000 * 2 ** Math.min(failures, 3)));
  }
  async function poll() {
    if (stopped) return;
    if (document.hidden || !navigator.onLine || requestRunning || navigationRunning) { schedule(); return; }
    requestRunning = true;
    const currentController = new AbortController();
    controller = currentController;
    const epoch = generation, timeout = setTimeout(() => currentController.abort(), 12000);
    try {
      const data = await jsonFetch(requestURL(), {signal:controller.signal});
      if (epoch !== generation) return;
      if (data.active_conversation) showActive(data.active_conversation, false);
      // Do not discard manually paged/scrolling conversation lists on each poll.
      const listScroll = list.parentElement;
      if (listScroll.scrollTop < 60) renderList(data);
      failures = 0;
      report(data.instagram_inbox_ready ? 'Inbox updates connected' : 'Connection needs attention — open settings');
    } catch (error) {
      if (epoch === generation && error.name !== 'AbortError') { failures += 1; report(error.message); }
    } finally {
      clearTimeout(timeout); requestRunning = false; if (epoch === generation) schedule();
    }
  }
  async function navigate(path, push = true) {
    generation += 1;
    navigationRunning = true;
    const epoch = generation;
    clearTimeout(timer); controller?.abort();
    controller = new AbortController();
    const signal = controller.signal;
    const timeout = setTimeout(() => { if (epoch === generation) controller.abort(); }, 12000);
    report('Loading conversation…');
    try {
      const url = requestURL(path);
      const data = await jsonFetch(url, {signal});
      if (epoch !== generation) return;
      showActive(data.active_conversation, active?.id !== data.active_conversation?.id); renderList(data);
      sendError(''); report('Inbox updates connected');
      if (push) history.pushState({}, '', url);
    } catch (error) {
      if (epoch === generation && error.name !== 'AbortError') report(error.message);
    } finally { clearTimeout(timeout); if (epoch === generation) { navigationRunning = false; schedule(); } }
  }
  list.addEventListener('click', event => {
    const row = event.target.closest('a[data-conversation-id]');
    if (!row || event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;
    event.preventDefault(); navigate(row.getAttribute('href'));
  });
  byId('ig-back').addEventListener('click', event => { event.preventDefault(); navigate(boot.list_url); });
  addEventListener('popstate', () => {
    search.value = new URL(location.href).searchParams.get('q') || '';
    navigate(location.pathname, false);
  });
  byId('ig-search-form').addEventListener('submit', event => { event.preventDefault(); navigate(active?.url || boot.list_url); });
  search.addEventListener('input', () => {
    clearTimeout(searchTimer); searchTimer = setTimeout(() => navigate(active?.url || boot.list_url), 350);
  });
  older.addEventListener('click', async () => {
    if (!active || !before || older.disabled) return;
    const epoch = generation; older.disabled = true;
    try {
      const data = await jsonFetch(requestURL(active.url, {before}), {signal:AbortSignal.timeout(12000)});
      if (epoch !== generation) return;
      upsertMessages(data.active_conversation.messages, true);
      before = data.active_conversation.before; older.hidden = !before;
    } catch (error) { report(error.message); } finally { older.disabled = false; }
  });
  more.addEventListener('click', async () => {
    if (nextOffset == null || more.disabled) return;
    const epoch = generation; more.disabled = true;
    try {
      const data = await jsonFetch(requestURL(boot.list_url, {offset:nextOffset}), {signal:AbortSignal.timeout(12000)});
      if (epoch === generation) renderList(data, true);
    } catch (error) { report(error.message); } finally { more.disabled = false; }
  });
  form.addEventListener('submit', async event => {
    event.preventDefault(); policy();
    if (!active || submit.disabled || !input.value.trim()) return;
    const conversation = active, text = input.value.trim();
    let request = pending.get(conversation.id);
    if (!request || request.body !== text) { request = {body:text, key:uuid()}; pending.set(conversation.id, request); }
    const body = new FormData(form);
    body.set('body', text); body.set('idempotency_key', request.key);
    sendRunning = true; policy(); sendError('');
    try {
      const result = await jsonFetch(conversation.send_url, {method:'POST', body, signal:AbortSignal.timeout(20000)});
      pending.delete(conversation.id);
      if (active?.id === conversation.id) {
        following = true; upsertMessages([result.message]); bottom();
        if (input.value.trim() === text) input.value = '';
        drafts.set(conversation.id, input.value);
      } else if ((drafts.get(conversation.id) || '').trim() === text) drafts.delete(conversation.id);
    } catch (error) {
      if (active?.id === conversation.id) sendError(error.message + ' Your draft is preserved.');
    } finally { sendRunning = false; policy(); schedule(); }
  });
  input.addEventListener('input', () => {
    if (active) drafts.set(active.id, input.value);
    input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 130) + 'px';
  });
  input.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); form.requestSubmit(); }
  });
  surface.addEventListener('scroll', () => { following = nearBottom(); }, {passive:true});
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { clearTimeout(timer); controller?.abort(); }
    else { policy(); poll(); }
  });
  addEventListener('offline', () => report('Offline — messages and drafts stay visible'));
  addEventListener('online', () => { failures = 0; poll(); });
  const policyTimer = setInterval(policy, 1000);
  addEventListener('pagehide', () => { stopped = true; controller?.abort(); clearTimeout(timer); clearTimeout(searchTimer); clearInterval(policyTimer); }, {once:true});
  formatTimes(shell); bindMedia(shell); policy();
  list.querySelectorAll('[data-conversation-id]').forEach(node => node.setAttribute('aria-current', String(node.dataset.conversationId === active?.id)));
  if (active) requestAnimationFrame(bottom);
  schedule();
})();
