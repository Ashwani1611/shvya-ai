"""UI wrappers for the WhatsApp inbox shell and failure diagnostics."""

from . import views_flat


_WHATSAPP_WEB_SHELL_STYLE = b"""
<style data-shvya-whatsapp-web-shell>
html.wa-chat-page,
body.wa-chat-page {
  height: 100%;
  overflow: hidden !important;
  overscroll-behavior: none;
}

.wa-page-host {
  min-height: 0 !important;
  overflow: hidden !important;
  overscroll-behavior: none;
  padding: 12px !important;
  background: #f0f2f5 !important;
}

.wa-web-shell {
  height: 100% !important;
  min-height: 0 !important;
  max-height: 100% !important;
  overflow: hidden !important;
  border: 1px solid #dfe3e5 !important;
  border-radius: 12px !important;
  background: #ffffff !important;
  box-shadow: 0 1px 3px rgba(11, 20, 26, 0.08) !important;
}

.wa-web-shell > * {
  min-height: 0 !important;
}

.wa-conversation-pane {
  width: 350px !important;
  min-width: 300px !important;
  background: #ffffff !important;
}

.wa-conversation-pane > div:first-child {
  background: #f0f2f5 !important;
  border-bottom-color: #e9edef !important;
}

.wa-conversation-pane .conversation-row {
  border-bottom-color: #f0f2f5 !important;
}

.wa-conversation-pane .conversation-row:hover {
  background: #f5f6f6 !important;
}

.wa-conversation-pane .conversation-row.bg-\[\#eaf7f0\] {
  background: #f0f2f5 !important;
}

.wa-thread-pane {
  min-width: 0 !important;
  min-height: 0 !important;
  background: #efeae2 !important;
}

.wa-thread-pane > div:first-child {
  min-height: 60px;
  background: #f0f2f5 !important;
  border-bottom-color: #dfe3e5 !important;
  box-shadow: none !important;
}

.wa-chat-surface {
  min-height: 0 !important;
  overscroll-behavior: contain;
  scrollbar-gutter: stable;
  background-color: #efeae2 !important;
  background-image:
    url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='260' height='260' viewBox='0 0 260 260'%3E%3Cg fill='none' stroke='%236f7d77' stroke-opacity='.075' stroke-width='1.15' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M18 28h20a8 8 0 0 1 8 8v9a8 8 0 0 1-8 8H29l-9 7V36a8 8 0 0 1-2-8z'/%3E%3Ccircle cx='91' cy='38' r='10'/%3E%3Cpath d='M86 38h10M91 33v10M145 23c10 4 17 12 19 23-11 2-20-4-24-14l5-9zM211 36c0 8-6 14-14 14-8 0-14-6-14-14s6-14 14-14c8 0 14 6 14 14zM190 36h14'/%3E%3Cpath d='M35 105c8-8 18-8 26 0M48 92v26M105 91l12 12-12 12-12-12 12-12zM165 86h23v18h-13l-6 6v-6h-4zM219 92c8 0 14 6 14 14s-6 14-14 14-14-6-14-14 6-14 14-14z'/%3E%3Cpath d='M22 173c12-8 24-8 36 0M40 160v25M91 161h25v19H91zM104 155v6M104 180v7M153 165c8-11 22-11 30 0-8 11-22 11-30 0zM213 155l8 8 8-8M221 163v23'/%3E%3Cpath d='M31 224l8-8 8 8-8 8-8-8zM86 221c7-8 18-8 25 0M98 211v21M154 215h21M164 205v21M218 212c8 0 14 6 14 14'/%3E%3C/g%3E%3C/svg%3E") !important;
  background-size: 260px 260px !important;
  background-repeat: repeat !important;
}

.wa-chat-surface .wa-bubble {
  border-radius: 8px !important;
  box-shadow: 0 1px 1px rgba(11, 20, 26, 0.13) !important;
}

.wa-chat-surface .wa-bubble.bg-\[\#d9fdd3\] {
  background: #d9fdd3 !important;
}

.wa-chat-surface .wa-bubble.bg-white {
  background: #ffffff !important;
}

.wa-chat-surface .wa-bubble.bg-\[\#fff1f0\] {
  background: #fff4f3 !important;
}

.wa-thread-pane > div:last-child {
  background: #f0f2f5 !important;
  border-top-color: #dfe3e5 !important;
}

.wa-thread-pane #message-body {
  border-color: transparent !important;
  background: #ffffff !important;
  box-shadow: none !important;
}

.wa-thread-pane #composer-form button[type='submit'],
.wa-context-pane .template-row button {
  background: #00a884 !important;
}

.wa-context-pane {
  width: 360px !important;
  min-width: 330px !important;
  min-height: 0 !important;
  background: #ffffff !important;
}

.wa-context-pane > div:first-child {
  background: #f0f2f5 !important;
  border-bottom-color: #e9edef !important;
}

.wa-context-pane .side-panel {
  min-height: 0 !important;
  overscroll-behavior: contain;
  scrollbar-gutter: stable;
  background: #ffffff;
}

.wa-web-shell .wa-scrollbar,
.wa-web-shell #thread,
.wa-web-shell .side-panel {
  overscroll-behavior: contain;
}

.wa-web-shell .wa-scrollbar::-webkit-scrollbar,
.wa-web-shell #thread::-webkit-scrollbar,
.wa-web-shell .side-panel::-webkit-scrollbar {
  width: 6px;
  height: 6px;
}

.wa-web-shell .wa-scrollbar::-webkit-scrollbar-thumb,
.wa-web-shell #thread::-webkit-scrollbar-thumb,
.wa-web-shell .side-panel::-webkit-scrollbar-thumb {
  background: rgba(11, 20, 26, 0.22);
  border-radius: 999px;
}

.wa-mobile-back {
  display: none;
  width: 36px;
  height: 36px;
  flex: 0 0 36px;
  align-items: center;
  justify-content: center;
  border-radius: 999px;
  color: #54656f;
}

.wa-mobile-back:hover {
  background: rgba(11, 20, 26, 0.06);
}

@media (max-width: 1399px) {
  .wa-conversation-pane {
    width: 320px !important;
  }
  .wa-context-pane {
    width: 340px !important;
    min-width: 320px !important;
  }
}

@media (max-width: 1199px) {
  .wa-conversation-pane {
    width: 300px !important;
  }
}

@media (max-width: 767px) {
  .wa-page-host {
    padding: 0 !important;
  }
  .wa-web-shell {
    border: 0 !important;
    border-radius: 0 !important;
    box-shadow: none !important;
  }
  .wa-web-shell.wa-has-active-chat .wa-conversation-pane {
    display: none !important;
  }
  .wa-web-shell.wa-empty-chat .wa-conversation-pane {
    display: flex !important;
    width: 100% !important;
    min-width: 0 !important;
  }
  .wa-web-shell.wa-empty-chat .wa-thread-pane {
    display: none !important;
  }
  .wa-mobile-back {
    display: inline-flex;
  }
  .wa-chat-surface {
    padding-left: 12px !important;
    padding-right: 12px !important;
  }
  .wa-chat-surface .wa-bubble {
    max-width: 86% !important;
  }
}
</style>
"""


_CHAT_UI_SCRIPT = b"""
<script data-shvya-whatsapp-chat-ui>
(function () {
  const thread = document.getElementById('thread');
  const surface = document.querySelector('.wa-chat-surface');
  const center = thread ? thread.closest('main') : (surface ? surface.closest('main') : null);

  if (center && center.parentElement) {
    const shell = center.parentElement;
    const host = shell.parentElement;
    const asides = shell.querySelectorAll(':scope > aside');

    document.documentElement.classList.add('wa-chat-page');
    document.body.classList.add('wa-chat-page');
    shell.classList.add('wa-web-shell');
    shell.classList.toggle('wa-has-active-chat', Boolean(thread));
    shell.classList.toggle('wa-empty-chat', !thread);
    center.classList.add('wa-thread-pane');

    if (host) host.classList.add('wa-page-host');
    if (asides.length) asides[0].classList.add('wa-conversation-pane');
    if (asides.length > 1) asides[asides.length - 1].classList.add('wa-context-pane');

    if (thread) {
      const header = center.firstElementChild;
      if (header && !header.querySelector('.wa-mobile-back')) {
        const back = document.createElement('a');
        back.className = 'wa-mobile-back';
        back.title = 'Back to chats';
        back.setAttribute('aria-label', 'Back to chats');
        back.innerHTML = '<i class="ti ti-arrow-left text-lg"></i>';
        back.href = window.location.pathname.replace(/chats\\/[^/]+\\/$/, 'chats/') + window.location.search;
        const headerInner = header.firstElementChild;
        if (headerInner) headerInner.insertBefore(back, headerInner.firstChild);
      }
    }
  }

  document.querySelectorAll('details').forEach(function (details) {
    const summary = details.querySelector(':scope > summary');
    if (!summary || summary.textContent.indexOf('Not sent') === -1) return;

    const heading = details.querySelector('.font-semibold.text-red-700');
    if (!heading) return;

    const diagnostic = heading.textContent.trim();
    if (!diagnostic) return;
    summary.textContent = 'Not sent \\u00b7 ' + diagnostic + ' \\u00b7 View details';
  });
})();
</script>
"""


def _inject_chat_ui(response):
    """Inject the WhatsApp Web-like shell into rendered chat HTML only."""
    content_type = response.get("Content-Type", "")
    if response.status_code != 200 or "text/html" not in content_type:
        return response

    content = response.content

    if b"data-shvya-whatsapp-web-shell" not in content:
        head_marker = b"</head>"
        if head_marker in content:
            content = content.replace(
                head_marker,
                _WHATSAPP_WEB_SHELL_STYLE + head_marker,
                1,
            )

    if b"data-shvya-whatsapp-chat-ui" not in content:
        body_marker = b"</body>"
        if body_marker in content:
            content = content.replace(
                body_marker,
                _CHAT_UI_SCRIPT + body_marker,
                1,
            )

    response.content = content
    if response.has_header("Content-Length"):
        response["Content-Length"] = str(len(response.content))
    return response


def _inject_failure_summary(response):
    """Backward-compatible alias used by the existing failure-details flow."""
    return _inject_chat_ui(response)


def whatsapp_chat_list_view(request):
    """Render the inbox list inside the locked WhatsApp-style shell."""
    return _inject_chat_ui(
        views_flat.whatsapp_chat_list_view(request)
    )


def whatsapp_chat_detail_view(request, lead_id):
    """Render a chat with the locked shell and exact failure diagnostics."""
    return _inject_chat_ui(
        views_flat.whatsapp_chat_detail_view(request, lead_id)
    )
