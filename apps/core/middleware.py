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
    gap: .7rem;
    pointer-events: none;
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display", "Segoe UI", sans-serif;
  }

  .shvya-toast {
    --shvya-toast-accent: 0, 122, 255;
    position: relative;
    isolation: isolate;
    pointer-events: auto;
    display: grid;
    grid-template-columns: 2.35rem minmax(0, 1fr) 1.75rem;
    align-items: start;
    column-gap: .8rem;
    overflow: hidden;
    border: 1px solid rgba(255, 255, 255, .72);
    border-radius: 1.35rem;
    background:
      linear-gradient(145deg, rgba(255, 255, 255, .91), rgba(247, 249, 252, .78));
    -webkit-backdrop-filter: blur(24px) saturate(180%);
    backdrop-filter: blur(24px) saturate(180%);
    padding: .9rem .9rem .9rem .95rem;
    box-shadow:
      0 1px 0 rgba(255, 255, 255, .9) inset,
      0 18px 52px rgba(15, 23, 42, .16),
      0 4px 14px rgba(15, 23, 42, .08);
    color: #1d1d1f;
    opacity: 0;
    transform: translate3d(0, -10px, 0) scale(.975);
    transform-origin: top right;
    transition:
      opacity .24s ease,
      transform .42s cubic-bezier(.22, 1, .36, 1),
      box-shadow .24s ease;
  }

  .shvya-toast::before {
    content: "";
    position: absolute;
    z-index: -1;
    inset: 0;
    pointer-events: none;
    background:
      radial-gradient(circle at 12% 2%, rgba(var(--shvya-toast-accent), .13), transparent 42%),
      linear-gradient(180deg, rgba(255,255,255,.35), transparent 38%);
  }

  .shvya-toast::after {
    content: "";
    position: absolute;
    left: 1.05rem;
    right: 1.05rem;
    bottom: .35rem;
    height: 2px;
    border-radius: 999px;
    background: linear-gradient(90deg, rgba(var(--shvya-toast-accent), .42), rgba(var(--shvya-toast-accent), .03));
    opacity: .72;
  }

  .shvya-toast.is-visible {
    opacity: 1;
    transform: translate3d(0, 0, 0) scale(1);
  }

  .shvya-toast.is-visible:hover {
    box-shadow:
      0 1px 0 rgba(255, 255, 255, .95) inset,
      0 22px 62px rgba(15, 23, 42, .19),
      0 6px 18px rgba(15, 23, 42, .09);
  }

  .shvya-toast.is-leaving {
    opacity: 0;
    transform: translate3d(8px, -7px, 0) scale(.975);
  }

  .shvya-toast__icon {
    display: inline-flex;
    width: 2.35rem;
    height: 2.35rem;
    align-items: center;
    justify-content: center;
    border: 1px solid rgba(var(--shvya-toast-accent), .18);
    border-radius: 999px;
    background: rgba(var(--shvya-toast-accent), .10);
    color: rgb(var(--shvya-toast-accent));
    box-shadow:
      0 1px 0 rgba(255,255,255,.72) inset,
      0 5px 12px rgba(var(--shvya-toast-accent), .09);
    font-size: 1rem;
    font-weight: 650;
    line-height: 1;
  }

  .shvya-toast__body {
    min-width: 0;
    padding-top: .08rem;
  }

  .shvya-toast__title {
    color: #1d1d1f;
    font-size: .9rem;
    font-weight: 650;
    letter-spacing: -.012em;
    line-height: 1.22rem;
  }

  .shvya-toast__message {
    margin-top: .18rem;
    color: rgba(29, 29, 31, .67);
    font-size: .8125rem;
    font-weight: 450;
    letter-spacing: -.006em;
    line-height: 1.2rem;
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
    background: rgba(118, 118, 128, .09);
    color: rgba(60, 60, 67, .55);
    cursor: pointer;
    padding: 0;
    font-size: 1rem;
    font-weight: 500;
    line-height: 1;
    transition: background .16s ease, color .16s ease, transform .16s ease;
  }

  .shvya-toast__close:hover {
    background: rgba(118, 118, 128, .16);
    color: rgba(29, 29, 31, .8);
    transform: scale(1.04);
  }

  .shvya-toast__close:focus-visible {
    outline: 3px solid rgba(0, 122, 255, .22);
    outline-offset: 2px;
  }

  .shvya-toast--success { --shvya-toast-accent: 40, 166, 73; }
  .shvya-toast--error { --shvya-toast-accent: 255, 59, 48; }
  .shvya-toast--warning { --shvya-toast-accent: 255, 149, 0; }
  .shvya-toast--info { --shvya-toast-accent: 0, 122, 255; }

  @media (prefers-color-scheme: dark) {
    .shvya-toast {
      border-color: rgba(255, 255, 255, .13);
      background: linear-gradient(145deg, rgba(45, 45, 48, .9), rgba(27, 27, 30, .84));
      color: #f5f5f7;
      box-shadow:
        0 1px 0 rgba(255, 255, 255, .09) inset,
        0 18px 54px rgba(0, 0, 0, .38),
        0 4px 16px rgba(0, 0, 0, .25);
    }
    .shvya-toast__title { color: #f5f5f7; }
    .shvya-toast__message { color: rgba(235, 235, 245, .68); }
    .shvya-toast__close {
      background: rgba(118, 118, 128, .18);
      color: rgba(235, 235, 245, .62);
    }
    .shvya-toast__close:hover {
      background: rgba(118, 118, 128, .28);
      color: rgba(255, 255, 255, .9);
    }
  }

  @media (max-width: 640px) {
    #shvya-toast-root {
      top: .75rem;
      right: .75rem;
      left: .75rem;
      width: auto;
    }
    .shvya-toast {
      grid-template-columns: 2.15rem minmax(0, 1fr) 1.65rem;
      column-gap: .7rem;
      border-radius: 1.2rem;
      padding: .82rem .82rem .84rem .86rem;
    }
    .shvya-toast__icon {
      width: 2.15rem;
      height: 2.15rem;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .shvya-toast,
    .shvya-toast__close {
      transition: none !important;
    }
  }
</style>
<script id="shvya-toast-script">
(function () {
  if (window.__shvyaToastReady) return;
  window.__shvyaToastReady = true;

  var TITLES = {
    success: 'Updated',
    error: 'Couldn\'t complete',
    warning: 'Needs attention',
    info: 'Update'
  };
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

  function pathOf(input) {
    try {
      return new URL(String(input || window.location.href), window.location.origin).pathname.toLowerCase();
    } catch (error) {
      return String(input || '').toLowerCase();
    }
  }

  function isAiSandboxUrl(input) {
    var path = pathOf(input);
    return /\/playground(?:\/|$)/.test(path) || /\/ai-sandbox(?:\/|$)/.test(path);
  }

  function isMajorMessage(message) {
    var text = String(message || '').trim().toLowerCase();
    if (!text) return false;

    if (/\b(settings?|preferences?)\b/.test(text) && /\b(saved|updated|changed)\b/.test(text)) return true;
    if (/\battributes?\b/.test(text) && /\b(created|added|updated|saved|deleted|removed|changed)\b/.test(text)) return true;
    if (/\bnotes?\b/.test(text) && /\b(added|updated|saved|deleted|removed|changed)\b/.test(text)) return true;
    if (/\bsequence\b/.test(text) && /\b(created|deleted)\b/.test(text)) return true;
    if (/\badded to (?:the )?sequence\b/.test(text) || /\bfollow-up added\b/.test(text)) return true;
    if (/\b(call log|call)\b/.test(text) && /\b(added|logged|removed|deleted)\b/.test(text)) return true;
    if (/\breminder\b/.test(text) && /\b(added|created|removed|deleted)\b/.test(text)) return true;
    if (/\b(workflow|smart trigger|trigger)\b/.test(text) && /\b(added|created|removed|deleted)\b/.test(text)) return true;

    if (text === 'ai settings saved successfully.') return true;
    if (text === 'ai-guided file added and processing started.') return true;
    if (text === 'file uploaded. processing has started.') return true;

    return false;
  }

  function titleFor(message, type) {
    if (type !== 'success') return TITLES[type];
    var text = String(message || '').toLowerCase();
    if (text.indexOf('setting') >= 0) return 'Settings updated';
    if (text.indexOf('attribute') >= 0) return 'Attribute updated';
    if (text.indexOf('note') >= 0) return 'Note updated';
    if (text.indexOf('sequence') >= 0 || text.indexOf('follow-up') >= 0) return 'Cadence updated';
    if (text.indexOf('call') >= 0) return 'Call log updated';
    if (text.indexOf('reminder') >= 0) return 'Reminder updated';
    if (text.indexOf('workflow') >= 0 || text.indexOf('trigger') >= 0) return 'Workflow updated';
    if (text.indexOf('ai') >= 0 || text.indexOf('file uploaded') >= 0) return 'AI Brain updated';
    return TITLES.success;
  }

  function majorMutationFor(input, method) {
    method = String(method || '').toUpperCase();
    if (!MUTATING[method]) return null;

    var path = pathOf(input);
    if (isAiSandboxUrl(path)) return null;

    if (/\/attributes\/create\/save\/?$/.test(path)) {
      return {message: 'Attribute added.', title: 'Attribute added'};
    }
    if (/\/attributes\/[^/]+\/edit\/save\/?$/.test(path)) {
      return {message: 'Attribute updated.', title: 'Attribute updated'};
    }
    if (/\/attributes\/[^/]+\/delete\/?$/.test(path)) {
      return {message: 'Attribute removed.', title: 'Attribute removed'};
    }
    if (/\/leads\/[^/]+\/attributes\/edit\/save\/?$/.test(path)) {
      return {message: 'Attributes updated.', title: 'Attributes updated'};
    }

    if (/\/leads\/[^/]+\/note\/save\/?$/.test(path)) {
      return {message: 'Note saved.', title: 'Note updated'};
    }
    if (/\/leads\/[^/]+\/note\/[^/]+\/(?:save|update|delete|remove)\/?$/.test(path)) {
      return {message: method === 'DELETE' ? 'Note removed.' : 'Note updated.', title: 'Note updated'};
    }

    if (/\/leads\/[^/]+\/call\/save\/?$/.test(path)) {
      return {message: 'Call log added.', title: 'Call log updated'};
    }
    if (/\/(?:call-logs?|calls)\/[^/]+\/(?:delete|remove)\/?$/.test(path)) {
      return {message: 'Call log removed.', title: 'Call log updated'};
    }

    if (/\/leads\/[^/]+\/reminder\/save\/?$/.test(path)) {
      return {message: 'Reminder added.', title: 'Reminder updated'};
    }
    if (/\/reminders\/[^/]+\/delete\/?$/.test(path)) {
      return {message: 'Reminder removed.', title: 'Reminder updated'};
    }

    if (/\/(?:smart-triggers|workflows)\/rules\/?$/.test(path) && method === 'POST') {
      return {message: 'Workflow added.', title: 'Workflow added'};
    }
    if (/\/(?:smart-triggers|workflows)\/rules\/[^/]+\/?$/.test(path) && method === 'DELETE') {
      return {message: 'Workflow removed.', title: 'Workflow removed'};
    }

    if (/\/settings(?:\/|$)/.test(path)) {
      return {message: 'Settings updated.', title: 'Settings updated'};
    }

    return null;
  }

  function requestUrlFromHtmx(detail) {
    detail = detail || {};
    var config = detail.requestConfig || {};
    if (config.path) return config.path;
    if (config.pathInfo && config.pathInfo.requestPath) return config.pathInfo.requestPath;
    if (detail.xhr && detail.xhr.responseURL) return detail.xhr.responseURL;
    return window.location.href;
  }

  function removeToast(node) {
    if (!node || node.dataset.closing === '1') return;
    node.dataset.closing = '1';
    node.classList.add('is-leaving');
    setTimeout(function () {
      if (node.parentNode) node.parentNode.removeChild(node);
    }, 260);
  }

  window.shvyaToast = function (message, type, options) {
    if (!message) return null;
    options = options || {};
    type = normaliseType(type);
    message = String(message).trim();
    if (!message) return null;

    if (isAiSandboxUrl(options.sourceUrl || options.url || '')) return null;
    if ((type === 'success' || type === 'info') && options.major !== true && !isMajorMessage(message)) {
      return null;
    }

    var dedupeKey = type + '|' + message;
    var now = Date.now();
    if (recent[dedupeKey] && now - recent[dedupeKey] < 1000) return null;
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
    title.textContent = options.title || titleFor(message, type);
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

    var duration = Number(options.duration || (type === 'error' ? 6000 : 4600));
    if (duration > 0) setTimeout(function () { removeToast(toast); }, duration);
    return toast;
  };

  window.addEventListener('shvya:toast', function (event) {
    var detail = event.detail || {};
    window.shvyaToast(detail.message || detail.text, detail.type || 'info', detail);
  });

  function convertDjangoMessages() {
    document.querySelectorAll('main > .mb-4.space-y-2').forEach(function (container) {
      var touched = false;
      Array.from(container.children).forEach(function (node) {
        if (!node.classList.contains('text-sm')) return;

        var type = null;
        if (node.classList.contains('bg-green-50')) type = 'success';
        else if (node.classList.contains('bg-red-50')) type = 'error';
        else if (node.classList.contains('bg-yellow-50') || node.classList.contains('bg-amber-50')) type = 'warning';
        else if (node.classList.contains('bg-gray-50') || node.classList.contains('bg-blue-50')) type = 'info';
        if (!type) return;

        touched = true;
        var message = String(node.textContent || '').trim();
        if (type !== 'success' || isMajorMessage(message)) {
          window.shvyaToast(message, type, {major: type === 'success'});
        }
        node.remove();
      });
      if (touched && !container.children.length) container.remove();
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
    var verb = String((detail.requestConfig && detail.requestConfig.verb) || '').toUpperCase();
    var url = requestUrlFromHtmx(detail);
    var major = majorMutationFor(url, verb);
    if (!major || isAiSandboxUrl(url)) return;
    window.shvyaToast(friendlyError(xhr ? xhr.status : 0), 'error', {sourceUrl: url});
  });

  document.addEventListener('htmx:sendError', function (event) {
    var detail = event.detail || {};
    var verb = String((detail.requestConfig && detail.requestConfig.verb) || '').toUpperCase();
    var url = requestUrlFromHtmx(detail);
    if (!majorMutationFor(url, verb) || isAiSandboxUrl(url)) return;
    window.shvyaToast('Network error. Please check your connection and try again.', 'error', {sourceUrl: url});
  });

  document.addEventListener('htmx:timeout', function (event) {
    var detail = event.detail || {};
    var verb = String((detail.requestConfig && detail.requestConfig.verb) || '').toUpperCase();
    var url = requestUrlFromHtmx(detail);
    if (!majorMutationFor(url, verb) || isAiSandboxUrl(url)) return;
    window.shvyaToast('The request took too long. Please try again.', 'warning', {sourceUrl: url});
  });

  document.addEventListener('htmx:afterRequest', function (event) {
    var detail = event.detail || {};
    var xhr = detail.xhr;
    var verb = String((detail.requestConfig && detail.requestConfig.verb) || '').toUpperCase();
    var url = requestUrlFromHtmx(detail);
    if (!xhr || xhr.status >= 400 || !MUTATING[verb] || isAiSandboxUrl(url)) return;

    var header = xhr.getResponseHeader('X-SHVYA-Toast');
    if (header === 'off') return;
    if (header) {
      window.shvyaToast(header, 'success', {major: true, sourceUrl: url});
      return;
    }

    var major = majorMutationFor(url, verb);
    if (major) {
      window.shvyaToast(major.message, 'success', {
        major: true,
        title: major.title,
        sourceUrl: url
      });
    }
  });

  var nativeFetch = window.fetch;
  if (nativeFetch) {
    window.fetch = function (input, init) {
      init = init || {};
      var method = String(init.method || (input && input.method) || 'GET').toUpperCase();
      var sourceUrl = typeof input === 'string' ? input : ((input && input.url) || window.location.href);
      var major = majorMutationFor(sourceUrl, method);

      return nativeFetch.apply(this, arguments).then(function (response) {
        if (!MUTATING[method] || isAiSandboxUrl(sourceUrl)) return response;

        var header = response.headers.get('X-SHVYA-Toast');
        if (header === 'off') return response;
        if (header) {
          if (response.ok) window.shvyaToast(header, 'success', {major: true, sourceUrl: sourceUrl});
          else window.shvyaToast(friendlyError(response.status), 'error', {sourceUrl: sourceUrl});
          return response;
        }

        if (!major) return response;
        if (response.ok) {
          window.shvyaToast(major.message, 'success', {
            major: true,
            title: major.title,
            sourceUrl: sourceUrl
          });
        } else {
          window.shvyaToast(friendlyError(response.status), 'error', {sourceUrl: sourceUrl});
        }
        return response;
      }).catch(function (error) {
        if (major && !isAiSandboxUrl(sourceUrl)) {
          window.shvyaToast('Network error. Please check your connection and try again.', 'error', {sourceUrl: sourceUrl});
        }
        throw error;
      });
    };
  }

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
