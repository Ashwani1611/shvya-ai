"""Progressive enhancement for the Meta API inbox.

The base WhatsApp shell stays server-rendered. This layer replaces only the inbox
shell for chat/tab/search/account navigation so the dashboard does not jump or
flash on every click.
"""

from .whatsapp_chat_failure_ui import _inject_chat_ui as _inject_base_chat_ui


_SMOOTH_INBOX_SCRIPT = b"""
<script data-shvya-whatsapp-smooth-ui>
(function () {
  const FILTER_PREFIXES = ['filter_', 'attr_'];
  let chatSocket = null;
  let chatSocketPath = '';
  let chatReconnect = null;

  function filterParams(source) {
    const output = new URLSearchParams();
    source.forEach(function (value, key) {
      if (FILTER_PREFIXES.some(function (prefix) { return key.indexOf(prefix) === 0; })) {
        output.set(key, value);
      }
    });
    return output;
  }

  function withCurrentFilters(url, preserveFilters) {
    const target = new URL(url, window.location.origin);
    const current = filterParams(new URL(window.location.href).searchParams);
    if (preserveFilters !== false) {
      current.forEach(function (value, key) {
        if (!target.searchParams.has(key)) target.searchParams.set(key, value);
      });
    }
    return target;
  }

  function shellFromDocument(doc) {
    const mains = doc.querySelectorAll('main');
    const center = mains.length ? mains[mains.length - 1] : null;
    return center && center.parentElement ? center.parentElement : null;
  }

  function decorateShell(shell) {
    if (!shell) return;
    shell.id = 'wa-web-shell';
    shell.classList.add('wa-web-shell');
    const center = shell.querySelector(':scope > main');
    const asides = shell.querySelectorAll(':scope > aside');
    const thread = shell.querySelector('#thread');
    if (center) center.classList.add('wa-thread-pane');
    if (asides.length) asides[0].classList.add('wa-conversation-pane');
    if (asides.length > 1) asides[asides.length - 1].classList.add('wa-context-pane');
    shell.classList.toggle('wa-has-active-chat', Boolean(thread));
    shell.classList.toggle('wa-empty-chat', !thread);
    if (thread) thread.scrollTop = thread.scrollHeight;
  }

  function showLoading(shell, enabled) {
    if (!shell) return;
    shell.style.transition = 'opacity 90ms ease';
    shell.style.opacity = enabled ? '0.72' : '1';
    shell.style.pointerEvents = enabled ? 'none' : '';
  }

  async function navigate(rawUrl, push, preserveFilters) {
    const shell = document.getElementById('wa-web-shell');
    if (!shell) {
      window.location.assign(rawUrl);
      return;
    }

    const target = withCurrentFilters(rawUrl, preserveFilters);
    showLoading(shell, true);
    try {
      const response = await fetch(target.toString(), {
        credentials: 'same-origin',
        headers: {'X-Requested-With': 'XMLHttpRequest'}
      });
      if (!response.ok) throw new Error('Unable to load WhatsApp chats');
      const html = await response.text();
      const doc = new DOMParser().parseFromString(html, 'text/html');
      const nextShell = shellFromDocument(doc);
      if (!nextShell) throw new Error('WhatsApp inbox shell missing');

      shell.innerHTML = nextShell.innerHTML;
      decorateShell(shell);
      if (window.htmx) window.htmx.process(shell);
      bindShell(shell);
      if (push !== false) history.pushState({shvyaWhatsAppInbox: true}, '', target.pathname + target.search);
    } catch (error) {
      window.location.assign(target.toString());
      return;
    } finally {
      showLoading(shell, false);
    }
  }

  function addFilterButton(pane) {
    if (!pane || pane.querySelector('[data-wa-crm-filters]')) return;
    const searchForm = pane.querySelector('form.relative');
    if (!searchForm || !searchForm.parentElement) return;

    const wrapper = document.createElement('div');
    wrapper.className = 'flex items-center gap-2';
    searchForm.parentElement.insertBefore(wrapper, searchForm);
    wrapper.appendChild(searchForm);
    searchForm.classList.add('min-w-0', 'flex-1');

    const button = document.createElement('button');
    button.type = 'button';
    button.setAttribute('data-wa-crm-filters', 'true');
    button.className = 'flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-gray-200 bg-white text-gray-500 transition hover:border-green-200 hover:bg-green-50 hover:text-[#128c7e]';
    button.title = 'CRM filters';
    button.setAttribute('aria-label', 'CRM filters');
    button.innerHTML = '<i class="ti ti-adjustments-horizontal"></i>';
    button.addEventListener('click', function () {
      const query = new URL(window.location.href).searchParams;
      query.set('surface', 'whatsapp');
      if (window.htmx) {
        window.htmx.ajax('GET', '/dashboard/leads/filters/?' + query.toString(), {
          target: '#modal-root',
          swap: 'innerHTML'
        });
      }
    });
    wrapper.appendChild(button);
  }

  function addActiveFilterChips(pane) {
    if (!pane) return;
    const existing = pane.querySelector('[data-wa-active-filters]');
    if (existing) existing.remove();
    const params = filterParams(new URL(window.location.href).searchParams);
    if (!Array.from(params.keys()).length) return;

    const searchArea = pane.querySelector(':scope > div:first-child');
    if (!searchArea) return;
    const row = document.createElement('div');
    row.setAttribute('data-wa-active-filters', 'true');
    row.className = 'mt-2 flex flex-wrap gap-1.5';
    params.forEach(function (value, key) {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'inline-flex max-w-full items-center gap-1 rounded-full border border-green-100 bg-green-50 px-2 py-1 text-[10px] font-medium text-[#128c7e]';
      const readable = key.replace(/^filter_/, '').replace(/^attr_/, '').replace(/_/g, ' ');
      chip.textContent = readable + ': ' + value + '  x';
      chip.addEventListener('click', function () {
        const url = new URL(window.location.href);
        url.searchParams.delete(key);
        // Do not re-inherit the removed filter from the current URL.
        navigate(url.toString(), true, false);
      });
      row.appendChild(chip);
    });
    searchArea.appendChild(row);
  }

  function bindShell(shell) {
    decorateShell(shell);
    const pane = shell.querySelector(':scope > aside');
    if (!pane || pane.dataset.smoothBound === '1') {
      addFilterButton(pane);
      addActiveFilterChips(pane);
      return;
    }
    pane.dataset.smoothBound = '1';
    addFilterButton(pane);
    addActiveFilterChips(pane);

    pane.addEventListener('click', function (event) {
      const anchor = event.target.closest('a[href]');
      if (!anchor) return;
      const target = new URL(anchor.href, window.location.origin);
      if (target.origin !== window.location.origin || target.pathname.indexOf('/dashboard/whatsapp/chats/') !== 0) return;
      event.preventDefault();
      navigate(target.toString(), true);
    });

    pane.addEventListener('submit', function (event) {
      const form = event.target.closest('form');
      if (!form) return;
      event.preventDefault();
      const target = new URL(form.action || window.location.href, window.location.origin);
      const formData = new FormData(form);
      target.search = '';
      formData.forEach(function (value, key) {
        if (String(value).trim()) target.searchParams.set(key, String(value));
      });
      const currentFilters = filterParams(new URL(window.location.href).searchParams);
      currentFilters.forEach(function (value, key) { target.searchParams.set(key, value); });
      navigate(target.toString(), true);
    });

    const accountSelect = pane.querySelector('#connected-account-select');
    if (accountSelect) {
      accountSelect.removeAttribute('onchange');
      accountSelect.addEventListener('change', function () {
        const form = accountSelect.form;
        if (form) form.requestSubmit();
      });
    }
    connectChatSocket();
  }

  function connectChatSocket() {
    const match = window.location.pathname.match(/\/dashboard\/whatsapp\/chats\/([0-9a-f-]+)\//i);
    const nextPath = match ? '/ws/whatsapp/' + match[1] + '/' : '/ws/whatsapp/inbox/';
    if (chatSocket && chatSocketPath === nextPath && chatSocket.readyState <= WebSocket.OPEN) return;
    if (chatSocket) chatSocket.close();
    chatSocketPath = nextPath;
    const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
    try {
      chatSocket = new WebSocket(protocol + '://' + window.location.host + nextPath);
      chatSocket.onmessage = function () {
        // The server remains the source of truth for message/media rendering.
        // Replace only the inbox shell, preserving the current filters/chat.
        navigate(window.location.href, false, false);
      };
      chatSocket.onclose = function () {
        if (chatSocketPath !== nextPath) return;
        clearTimeout(chatReconnect);
        chatReconnect = setTimeout(connectChatSocket, 1500);
      };
    } catch (_) {
      clearTimeout(chatReconnect);
      chatReconnect = setTimeout(connectChatSocket, 1500);
    }
  }

  document.addEventListener('submit', function (event) {
    const form = event.target.closest('form[data-whatsapp-filter-form="true"]');
    if (!form) return;
    event.preventDefault();
    const target = new URL(form.action, window.location.origin);
    const data = new FormData(form);
    data.forEach(function (value, key) {
      if (String(value).trim()) target.searchParams.set(key, String(value));
    });
    const modalRoot = document.getElementById('modal-root');
    if (modalRoot) modalRoot.innerHTML = '';
    navigate(target.toString(), true);
  });

  window.addEventListener('popstate', function () {
    if (document.getElementById('wa-web-shell')) navigate(window.location.href, false);
  });

  const initial = (function () {
    const thread = document.getElementById('thread');
    const surface = document.querySelector('.wa-chat-surface');
    const center = thread ? thread.closest('main') : (surface ? surface.closest('main') : null);
    return center && center.parentElement ? center.parentElement : null;
  })();
  if (initial) {
    decorateShell(initial);
    bindShell(initial);
  }

  connectChatSocket();

  window.shvyaWhatsAppNavigate = navigate;
})();
</script>
"""


def _inject_chat_ui(response):
    response = _inject_base_chat_ui(response)
    content_type = response.get("Content-Type", "")
    if response.status_code != 200 or "text/html" not in content_type:
        return response

    content = response.content
    if b"data-shvya-whatsapp-smooth-ui" not in content:
        marker = b"</body>"
        if marker in content:
            content = content.replace(marker, _SMOOTH_INBOX_SCRIPT + marker, 1)
            response.content = content
            if response.has_header("Content-Length"):
                response["Content-Length"] = str(len(content))
    return response
