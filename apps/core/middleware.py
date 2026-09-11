"""Shared response middleware for SHVYA UI behaviour."""


TOAST_ASSET = r'''
<style id="shvya-toast-styles">
  #shvya-toast-root {
    position: fixed;
    top: 1rem;
    right: 1rem;
    z-index: 99999;
    display: flex;
    width: min(25rem, calc(100vw - 2rem));
    flex-direction: column;
    gap: .625rem;
    pointer-events: none;
  }
  .shvya-toast {
    pointer-events: auto;
    position: relative;
    display: grid;
    grid-template-columns: 2rem minmax(0, 1fr) 1.75rem;
    align-items: start;
    gap: .75rem;
    overflow: hidden;
    border: 1px solid rgba(0, 0, 0, .08);
    border-radius: 1.125rem;
    background: rgba(250, 250, 252, .88);
    -webkit-backdrop-filter: saturate(180%) blur(24px);
    backdrop-filter: saturate(180%) blur(24px);
    padding: .875rem .875rem .875rem .9rem;
    box-shadow:
      0 18px 48px rgba(0, 0, 0, .14),
      0 4px 14px rgba(0, 0, 0, .06),
      inset 0 1px 0 rgba(255, 255, 255, .72);
    color: #1d1d1f;
    opacity: 0;
    transform: translateY(-8px) scale(.975);
    transform-origin: top right;
    transition:
      opacity .24s cubic-bezier(.22, 1, .36, 1),
      transform .28s cubic-bezier(.22, 1, .36, 1);
  }
  .shvya-toast::before {
    content: "";
    position: absolute;
    inset: 0 0 auto 0;
    height: 1px;
    background: linear-gradient(90deg, transparent, rgba(255, 255, 255, .92), transparent);
    pointer-events: none;
  }
  .shvya-toast.is-visible {
    opacity: 1;
    transform: translateY(0) scale(1);
  }
  .shvya-toast.is-leaving {
    opacity: 0;
    transform: translateY(-6px) scale(.985);
  }
  .shvya-toast__icon {
    display: inline-flex;
    width: 2rem;
    height: 2rem;
    align-items: center;
    justify-content: center;
    border-radius: 999px;
    font-size: .9rem;
    font-weight: 700;
    line-height: 1;
    letter-spacing: -.02em;
  }
  .shvya-toast__body {
    min-width: 0;
    padding-top: .05rem;
  }
  .shvya-toast__title {
    font-size: .875rem;
    font-weight: 650;
    line-height: 1.25rem;
    letter-spacing: -.01em;
    color: #1d1d1f;
  }
  .shvya-toast__message {
    margin-top: .125rem;
    font-size: .8125rem;
    line-height: 1.25rem;
    letter-spacing: -.003em;
    color: #6e6e73;
    overflow-wrap: anywhere;
  }
  .shvya-toast__close {
    display: inline-flex;
    width: 1.75rem;
    height: 1.75rem;
    align-items: center;
    justify-content: center;
    border: 0;
    border-radius: 999px;
    background: transparent;
    color: #86868b;
    cursor: pointer;
    padding: 0;
    font-size: 1rem;
    line-height: 1;
    transition: background-color .16s ease, color .16s ease, transform .16s ease;
  }
  .shvya-toast__close:hover {
    background: rgba(0, 0, 0, .055);
    color: #1d1d1f;
  }
  .shvya-toast__close:active { transform: scale(.94); }
  .shvya-toast__close:focus-visible {
    outline: 2px solid #0071e3;
    outline-offset: 2px;
  }
  .shvya-toast--success .shvya-toast__icon {
    color: #1f7a35;
    background: rgba(52, 199, 89, .13);
  }
  .shvya-toast--error .shvya-toast__icon {
    color: #d70015;
    background: rgba(255, 59, 48, .12);
  }
  .shvya-toast--warning .shvya-toast__icon {
    color: #a05a00;
    background: rgba(255, 159, 10, .15);
  }
  .shvya-toast--info .shvya-toast__icon {
    color: #0066cc;
    background: rgba(0, 113, 227, .11);
  }
  @media (max-width: 640px) {
    #shvya-toast-root {
      top: .75rem;
      right: .75rem;
      left: .75rem;
      width: auto;
    }
    .shvya-toast {
      border-radius: 1rem;
      padding: .825rem;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .shvya-toast,
    .shvya-toast__close {
      transition: none;
    }
  }
</style>
<script id="shvya-toast-script">
(function () {
  if (window.__shvyaToastReady) return;
  window.__shvyaToastReady = true;

  var TITLES = {success: 'Updated', error: 'Couldn’t complete', warning: 'Attention', info: 'Updated'};
  var ICONS = {success: '✓', error: '!', warning: '!', info: 'i'};
  var MUTATING = {POST: true, PUT: true, PATCH: true, DELETE: true};
  var recent = Object.create(null);

  function root() {
    var node = document.getElementById('shvya-toast-root');
    if (!node) {
      node = document.createElement('div');
      node.id = 'shvya-toast-root';
      node.setAttribute('aria-live', 'polite');
      node.setAttribute('aria-atomic', 'false');
      document.body.appendChild(node);
    }
    return node;
  }

  function normaliseType(type) {
    type = String(type || 'info').toLowerCase();
    if (type === 'danger' || type === 'failed' || type === 'failure') return 'error';
    if (type === 'warn') return 'warning';
    return ['success', 'error', 'warning', 'info'].indexOf(type) >= 0 ? type : 'info';
  }

  function requestPath(input) {
    var raw = '';
    if (typeof input === 'string') raw = input;
    else if (input && typeof input.url === 'string') raw = input.url;
    else if (input && typeof input.path === 'string') raw = input.path;
    if (!raw) return '';
    try { return new URL(raw, window.location.href).pathname.toLowerCase(); }
    catch (error) { return String(raw).split('?')[0].toLowerCase(); }
  }

  function isSandboxRequest(input) {
    return requestPath(input).indexOf('/api/v1/ai-engagement/playground/') !== -1;
  }

  function isAiBrainPage() {
    var path = String(window.location.pathname || '').toLowerCase();
    return path.indexOf('/dashboard/knowledge-base/ai-setup/') !== -1 ||
      path.indexOf('/dashboard/playbooks/') !== -1 ||
      path.indexOf('/dashboard/ai-brain/') !== -1;
  }

  function isMajorChangeMessage(message) {
    var text = String(message || '').trim().toLowerCase();
    if (!text) return false;

    if (/(settings?|configuration)/.test(text) &&
        /(saved|updated|changed|enabled|disabled|turned on|turned off)/.test(text)) return true;

    if (/\battributes?\b/.test(text) &&
        /(created|added|updated|changed|saved|deleted|removed)/.test(text)) return true;

    if (/\bnotes?\b/.test(text) &&
        /(added|updated|changed|saved|deleted|removed)/.test(text)) return true;

    if (/\bsequence\b/.test(text) &&
        /(created|deleted|removed)/.test(text)) return true;

    if (/(follow[- ]?ups?|added to (?:the )?sequence)/.test(text) &&
        /(added|created)/.test(text)) return true;

    if (/(call log|call logged)/.test(text) &&
        /(added|created|logged|removed|deleted)/.test(text)) return true;

    if (/\breminders?\b/.test(text) &&
        /(added|created|removed|deleted)/.test(text)) return true;

    if (/(workflows?|smart triggers?)/.test(text) &&
        /(added|created|removed|deleted)/.test(text)) return true;

    if (isAiBrainPage()) {
      if (/\bprompt\b/.test(text) && /(added|created|saved|updated)/.test(text)) return true;
      if (/\babout\b/.test(text) && /(added|saved|updated|changed)/.test(text)) return true;
      if (/\bfile\b/.test(text) && /(added|uploaded)/.test(text)) return true;
      if (/\bai settings\b/.test(text) && /(saved|updated)/.test(text)) return true;
    }

    return false;
  }

  function isMajorMutation(method, input) {
    method = String(method || '').toUpperCase();
    if (!MUTATING[method]) return false;

    var path = requestPath(input);
    if (!path || isSandboxRequest(path)) return false;

    if (path.indexOf('/settings/') !== -1) return true;
    if (path.indexOf('/dashboard/attributes/') !== -1) return true;
    if (/\/dashboard\/leads\/[^/]+\/attributes\/edit\/save\/?$/.test(path)) return true;
    if (/\/dashboard\/leads\/[^/]+\/note\/save\/?$/.test(path)) return true;
    if (/\/dashboard\/leads\/[^/]+\/call\/save\/?$/.test(path)) return true;
    if (/\/dashboard\/leads\/[^/]+\/reminder\/save\/?$/.test(path)) return true;
    if (/\/dashboard\/reminders\/[^/]+\/delete\/?$/.test(path)) return true;

    if (path.indexOf('/dashboard/cadence/sequences/') === 0) {
      if (/\/new\/save\/?$/.test(path)) return true;
      if (/\/delete\/?$/.test(path)) return true;
      if (/\/(templates|whatsapp|email|reminder)\/add\/?$/.test(path)) return true;
    }

    if (path.indexOf('/dashboard/workflows/rules/') === 0) {
      if (method === 'POST' && /\/dashboard\/workflows\/rules\/?$/.test(path)) return true;
      if (method === 'DELETE') return true;
    }

    return false;
  }

  function shouldRenderToast(message, type, options) {
    options = options || {};
    if (options.force === true) return true;
    if (type === 'error' || type === 'warning') return true;
    return isMajorChangeMessage(message);
  }

  function removeToast(node) {
    if (!node || node.dataset.closing === '1') return;
    node.dataset.closing = '1';
    node.classList.add('is-leaving');
    setTimeout(function () { if (node.parentNode) node.parentNode.removeChild(node); }, 290);
  }

  window.shvyaToast = function (message, type, options) {
    if (!message) return null;
    options = options || {};
    type = normaliseType(type);
    message = String(message).trim();
    if (!message || !shouldRenderToast(message, type, options)) return null;

    var dedupeKey = type + '|' + message;
    var now = Date.now();
    if (recent[dedupeKey] && now - recent[dedupeKey] < 1200) return null;
    recent[dedupeKey] = now;

    var toast = document.createElement('div');
    toast.className = 'shvya-toast shvya-toast--' + type;
    toast.setAttribute('role', type === 'error' ? 'alert' : 'status');

    var icon = document.createElement('div');
    icon.className = 'shvya-toast__icon';
    icon.setAttribute('aria-hidden', 'true');
    icon.textContent = ICONS[type];

    var body = document.createElement('div');
    body.className = 'shvya-toast__body';
    var title = document.createElement('div');
    title.className = 'shvya-toast__title';
    title.textContent = options.title || TITLES[type];
    var text = document.createElement('div');
    text.className = 'shvya-toast__message';
    text.textContent = message;
    body.appendChild(title);
    body.appendChild(text);

    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'shvya-toast__close';
    close.setAttribute('aria-label', 'Close notification');
    close.textContent = '×';
    close.addEventListener('click', function () { removeToast(toast); });

    toast.appendChild(icon);
    toast.appendChild(body);
    toast.appendChild(close);
    root().appendChild(toast);
    requestAnimationFrame(function () { toast.classList.add('is-visible'); });

    var duration = Number(options.duration || (type === 'error' ? 6000 : 4200));
    if (duration > 0) setTimeout(function () { removeToast(toast); }, duration);
    return toast;
  };

  window.addEventListener('shvya:toast', function (event) {
    var detail = event.detail || {};
    window.shvyaToast(detail.message || detail.text, detail.type || 'info', detail);
  });

  function convertDjangoMessages() {
    document.querySelectorAll('main > .mb-4.space-y-2').forEach(function (container) {
      var recognized = false;
      Array.from(container.children).forEach(function (node) {
        if (!node.classList.contains('text-sm')) return;
        var type = null;
        if (node.classList.contains('bg-green-50')) type = 'success';
        else if (node.classList.contains('bg-red-50')) type = 'error';
        else if (node.classList.contains('bg-yellow-50') || node.classList.contains('bg-amber-50')) type = 'warning';
        else if (node.classList.contains('bg-gray-50') || node.classList.contains('bg-blue-50')) type = 'info';
        if (!type) return;
        recognized = true;
        window.shvyaToast(node.textContent, type);
      });
      if (recognized) container.remove();
    });
  }

  function friendlyError(status) {
    if (status === 400) return 'The request could not be completed. Please check the information and try again.';
    if (status === 401 || status === 403) return 'You do not have permission to complete this action.';
    if (status === 404) return 'The requested item could not be found.';
    if (status === 409) return 'This change conflicts with newer data. Please refresh and try again.';
    if (status === 429) return 'Too many requests. Please wait a moment and try again.';
    if (status >= 500) return 'The server could not complete the request. Please try again shortly.';
    return 'The action could not be completed. Please try again.';
  }

  document.addEventListener('htmx:responseError', function (event) {
    var detail = event.detail || {};
    var xhr = detail.xhr;
    var requestConfig = detail.requestConfig || {};
    if (isSandboxRequest(requestConfig.path || '')) return;
    window.shvyaToast(friendlyError(xhr ? xhr.status : 0), 'error');
  });
  document.addEventListener('htmx:sendError', function (event) {
    var detail = event.detail || {};
    var requestConfig = detail.requestConfig || {};
    if (isSandboxRequest(requestConfig.path || '')) return;
    window.shvyaToast('Network error. Please check your connection and try again.', 'error');
  });
  document.addEventListener('htmx:timeout', function (event) {
    var detail = event.detail || {};
    var requestConfig = detail.requestConfig || {};
    if (isSandboxRequest(requestConfig.path || '')) return;
    window.shvyaToast('The request took too long. Please try again.', 'warning');
  });
  document.addEventListener('htmx:afterRequest', function (event) {
    var detail = event.detail || {};
    var xhr = detail.xhr;
    var requestConfig = detail.requestConfig || {};
    var verb = String(requestConfig.verb || '').toUpperCase();
    var path = requestConfig.path || '';
    if (!xhr || xhr.status >= 400 || !MUTATING[verb] || isSandboxRequest(path)) return;

    var header = xhr.getResponseHeader('X-SHVYA-Toast');
    if (header === 'off') return;
    if (header) {
      window.shvyaToast(header, 'success', {force: true});
      return;
    }
    if (isMajorMutation(verb, path)) {
      window.shvyaToast('Changes saved successfully.', 'success', {force: true});
    }
  });

  var nativeFetch = window.fetch;
  if (nativeFetch) {
    window.fetch = function (input, init) {
      init = init || {};
      var method = String(init.method || (input && input.method) || 'GET').toUpperCase();
      var sandboxRequest = isSandboxRequest(input);
      return nativeFetch.apply(this, arguments).then(function (response) {
        if (sandboxRequest || !MUTATING[method]) return response;

        var header = response.headers.get('X-SHVYA-Toast');
        if (header === 'off') return response;

        if (response.ok) {
          if (header) window.shvyaToast(header, 'success', {force: true});
          else if (isMajorMutation(method, input)) {
            window.shvyaToast('Changes saved successfully.', 'success', {force: true});
          }
        } else {
          window.shvyaToast(friendlyError(response.status), 'error');
        }
        return response;
      }).catch(function (error) {
        if (!sandboxRequest && MUTATING[method]) {
          window.shvyaToast('Network error. Please check your connection and try again.', 'error');
        }
        throw error;
      });
    };
  }

  window.addEventListener('error', function () {
    window.shvyaToast('Something unexpected happened on this page. Please try again.', 'error');
  });
  window.addEventListener('unhandledrejection', function () {
    window.shvyaToast('Something unexpected happened. Please try again.', 'error');
  });

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', convertDjangoMessages);
  else convertDjangoMessages();
})();
</script>
'''


class GlobalToastMiddleware:
    """Inject SHVYA's global toast UI into regular HTML responses.

    This deliberately leaves JSON/API/file/streaming responses untouched. It also
    skips encoded responses because modifying gzip/brotli bytes here would corrupt
    them.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        content_type = response.get("Content-Type", "")
        if (
            getattr(response, "streaming", False)
            or "text/html" not in content_type.lower()
            or response.get("Content-Encoding")
            or response.status_code in (204, 304)
        ):
            return response

        try:
            html = response.content.decode(response.charset or "utf-8")
        except (AttributeError, UnicodeDecodeError):
            return response

        if "</body>" not in html.lower() or "id=\"shvya-toast-script\"" in html:
            return response

        lower_html = html.lower()
        index = lower_html.rfind("</body>")
        html = html[:index] + TOAST_ASSET + html[index:]
        response.content = html.encode(response.charset or "utf-8")
        if response.has_header("Content-Length"):
            response["Content-Length"] = str(len(response.content))
        return response
