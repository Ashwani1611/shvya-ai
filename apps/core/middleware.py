"""Shared response middleware for SHVYA UI behaviour."""


TOAST_ASSET = r'''
<style id="shvya-toast-styles">
  #shvya-toast-root {
    position: fixed;
    top: 1rem;
    right: 1rem;
    z-index: 99999;
    display: flex;
    width: min(26rem, calc(100vw - 2rem));
    flex-direction: column;
    gap: .75rem;
    pointer-events: none;
  }
  .shvya-toast {
    pointer-events: auto;
    display: flex;
    align-items: flex-start;
    gap: .75rem;
    border: 1px solid #e5e7eb;
    border-left-width: 4px;
    border-radius: .75rem;
    background: #fff;
    padding: .875rem 1rem;
    box-shadow: 0 16px 40px rgba(15, 23, 42, .14);
    color: #1f2937;
    opacity: 0;
    transform: translateX(18px);
    transition: opacity .18s ease, transform .18s ease;
  }
  .shvya-toast.is-visible { opacity: 1; transform: translateX(0); }
  .shvya-toast.is-leaving { opacity: 0; transform: translateX(18px); }
  .shvya-toast__icon { flex: 0 0 auto; font-size: 1.25rem; line-height: 1.25rem; }
  .shvya-toast__body { min-width: 0; flex: 1; }
  .shvya-toast__title { font-size: .875rem; font-weight: 700; line-height: 1.25rem; }
  .shvya-toast__message { margin-top: .125rem; font-size: .8125rem; line-height: 1.25rem; color: #4b5563; overflow-wrap: anywhere; }
  .shvya-toast__close { flex: 0 0 auto; border: 0; background: transparent; color: #9ca3af; cursor: pointer; padding: 0; font-size: 1.125rem; line-height: 1.25rem; }
  .shvya-toast__close:hover { color: #374151; }
  .shvya-toast--success { border-left-color: #16a34a; }
  .shvya-toast--success .shvya-toast__icon, .shvya-toast--success .shvya-toast__title { color: #15803d; }
  .shvya-toast--error { border-left-color: #dc2626; }
  .shvya-toast--error .shvya-toast__icon, .shvya-toast--error .shvya-toast__title { color: #b91c1c; }
  .shvya-toast--warning { border-left-color: #d97706; }
  .shvya-toast--warning .shvya-toast__icon, .shvya-toast--warning .shvya-toast__title { color: #b45309; }
  .shvya-toast--info { border-left-color: #2563eb; }
  .shvya-toast--info .shvya-toast__icon, .shvya-toast--info .shvya-toast__title { color: #1d4ed8; }
  @media (max-width: 640px) {
    #shvya-toast-root { top: .75rem; right: .75rem; left: .75rem; width: auto; }
  }
</style>
<script id="shvya-toast-script">
(function () {
  if (window.__shvyaToastReady) return;
  window.__shvyaToastReady = true;

  var TITLES = {success: 'Success', error: 'Something went wrong', warning: 'Attention', info: 'Update'};
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

  function removeToast(node) {
    if (!node || node.dataset.closing === '1') return;
    node.dataset.closing = '1';
    node.classList.add('is-leaving');
    setTimeout(function () { if (node.parentNode) node.parentNode.removeChild(node); }, 190);
  }

  window.shvyaToast = function (message, type, options) {
    if (!message) return null;
    options = options || {};
    type = normaliseType(type);
    message = String(message).trim();
    if (!message) return null;

    var dedupeKey = type + '|' + message;
    var now = Date.now();
    if (recent[dedupeKey] && now - recent[dedupeKey] < 900) return null;
    recent[dedupeKey] = now;

    var toast = document.createElement('div');
    toast.className = 'shvya-toast shvya-toast--' + type;
    toast.setAttribute('role', type === 'error' ? 'alert' : 'status');

    var icon = document.createElement('div');
    icon.className = 'shvya-toast__icon';
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

    var duration = Number(options.duration || (type === 'error' ? 6000 : 4000));
    if (duration > 0) setTimeout(function () { removeToast(toast); }, duration);
    return toast;
  };

  window.addEventListener('shvya:toast', function (event) {
    var detail = event.detail || {};
    window.shvyaToast(detail.message || detail.text, detail.type || 'info', detail);
  });

  function convertDjangoMessages() {
    document.querySelectorAll('main > .mb-4.space-y-2').forEach(function (container) {
      var converted = false;
      Array.from(container.children).forEach(function (node) {
        if (!node.classList.contains('text-sm')) return;
        var type = null;
        if (node.classList.contains('bg-green-50')) type = 'success';
        else if (node.classList.contains('bg-red-50')) type = 'error';
        else if (node.classList.contains('bg-yellow-50') || node.classList.contains('bg-amber-50')) type = 'warning';
        else if (node.classList.contains('bg-gray-50') || node.classList.contains('bg-blue-50')) type = 'info';
        if (!type) return;
        converted = true;
        window.shvyaToast(node.textContent, type);
      });
      if (converted) container.remove();
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
    var xhr = event.detail && event.detail.xhr;
    window.shvyaToast(friendlyError(xhr ? xhr.status : 0), 'error');
  });
  document.addEventListener('htmx:sendError', function () {
    window.shvyaToast('Network error. Please check your connection and try again.', 'error');
  });
  document.addEventListener('htmx:timeout', function () {
    window.shvyaToast('The request took too long. Please try again.', 'warning');
  });
  document.addEventListener('htmx:afterRequest', function (event) {
    var detail = event.detail || {};
    var xhr = detail.xhr;
    var verb = String((detail.requestConfig && detail.requestConfig.verb) || '').toUpperCase();
    if (xhr && xhr.status < 400 && MUTATING[verb] && xhr.getResponseHeader('X-SHVYA-Toast') !== 'off') {
      window.shvyaToast(xhr.getResponseHeader('X-SHVYA-Toast') || 'Changes saved successfully.', 'success');
    }
  });

  var nativeFetch = window.fetch;
  if (nativeFetch) {
    window.fetch = function (input, init) {
      init = init || {};
      var method = String(init.method || (input && input.method) || 'GET').toUpperCase();
      return nativeFetch.apply(this, arguments).then(function (response) {
        if (MUTATING[method] && response.headers.get('X-SHVYA-Toast') !== 'off') {
          if (response.ok) window.shvyaToast(response.headers.get('X-SHVYA-Toast') || 'Changes saved successfully.', 'success');
          else window.shvyaToast(friendlyError(response.status), 'error');
        }
        return response;
      }).catch(function (error) {
        if (MUTATING[method]) window.shvyaToast('Network error. Please check your connection and try again.', 'error');
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
