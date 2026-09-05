"""WhatsApp-only visual theme for SHVYA.

This middleware is intentionally scoped to /dashboard/whatsapp/ so the CRM,
Co-Pilot, Instagram and the rest of SHVYA keep their normal blue identity.
"""

WHATSAPP_THEME = r'''
<style id="shvya-whatsapp-theme">
/* WhatsApp module palette: green for channel actions, neutral surfaces. */
main .bg-blue-600 { background-color: #128c7e !important; }
main .hover\:bg-blue-700:hover { background-color: #0f766e !important; }
main .bg-blue-50\/60,
main .bg-blue-50\/50,
main .bg-blue-50 { background-color: #ecfdf5 !important; }
main .text-blue-600,
main .text-blue-700 { color: #047857 !important; }
main .border-blue-200,
main .border-blue-500 { border-color: #a7f3d0 !important; }
main .hover\:text-blue-600:hover,
main .hover\:text-blue-700:hover { color: #047857 !important; }
main .hover\:bg-blue-50:hover,
main .hover\:bg-blue-100:hover { background-color: #d1fae5 !important; }
main .focus\:border-blue-500:focus { border-color: #10b981 !important; }
main .focus\:ring-blue-500:focus { --tw-ring-color: #10b981 !important; }
main .has-\[\:checked\]\:border-blue-500:has(:checked) { border-color: #10b981 !important; }
main .has-\[\:checked\]\:bg-blue-50\/50:has(:checked) { background-color: #ecfdf5 !important; }

/* Keep WhatsApp navigation visibly WhatsApp, without recolouring other modules. */
aside a[href*="/dashboard/whatsapp/"][class*="bg-blue-50"] {
    background-color: #ecfdf5 !important;
    color: #047857 !important;
}
aside a[href*="/dashboard/whatsapp/"]:hover {
    color: #047857 !important;
}

/* Native form controls and radio/checkbox accents on WhatsApp pages. */
main input[type="checkbox"], main input[type="radio"] { accent-color: #128c7e; }
main a:focus-visible, main button:focus-visible, main input:focus-visible,
main select:focus-visible, main textarea:focus-visible { outline-color: #10b981; }
</style>
'''

WHATSAPP_CHAT_PIPELINE_UI = r'''
<script id="shvya-whatsapp-pipeline-ui">
(function () {
    if (!window.location.pathname.startsWith('/dashboard/whatsapp/chats/')) return;

    var form = document.getElementById('lead-quick-form');
    if (!form) return;

    var quickUrl = form.getAttribute('action') || '';
    var match = quickUrl.match(/\/dashboard\/whatsapp\/leads\/([^/]+)\/quick-update\//);
    if (!match) return;

    var leadId = match[1];
    var optionsUrl = '/dashboard/whatsapp/leads/' + leadId + '/pipeline-options/';

    var pipelineLabel = Array.from(form.querySelectorAll('p')).find(function (node) {
        return node.textContent.trim().toLowerCase() === 'pipeline';
    });
    if (!pipelineLabel) return;

    var pipelineCard = pipelineLabel.closest('.rounded-xl');
    if (!pipelineCard) return;

    var currentRow = pipelineCard.querySelector('.mt-1.flex');
    if (!currentRow) return;

    var changeLink = Array.from(currentRow.querySelectorAll('a')).find(function (node) {
        return node.textContent.trim().toLowerCase() === 'change';
    });
    if (!changeLink) return;

    changeLink.addEventListener('click', function (event) {
        event.preventDefault();
        event.stopPropagation();

        if (pipelineCard.querySelector('#lead-pipeline-inline')) return;

        changeLink.textContent = 'Loading...';

        fetch(optionsUrl, {headers: {'X-Requested-With': 'XMLHttpRequest'}})
            .then(function (response) {
                if (!response.ok) throw new Error('Could not load pipelines.');
                return response.json();
            })
            .then(function (data) {
                var select = document.createElement('select');
                select.id = 'lead-pipeline-inline';
                select.className = 'mt-1 w-full rounded-lg border border-green-200 bg-white px-2 py-1.5 text-xs text-gray-700 outline-none focus:border-green-500 focus:ring-2 focus:ring-green-100';

                (data.pipelines || []).forEach(function (pipeline) {
                    var option = document.createElement('option');
                    option.value = pipeline.id;
                    option.textContent = pipeline.name;
                    option.selected = pipeline.id === data.pipeline_id;
                    select.appendChild(option);
                });

                currentRow.replaceWith(select);

                select.addEventListener('change', function () {
                    var body = new FormData();
                    body.append('pipeline', select.value);

                    var csrfInput = form.querySelector('input[name="csrfmiddlewaretoken"]');
                    var headers = {'X-Requested-With': 'XMLHttpRequest'};
                    if (csrfInput) headers['X-CSRFToken'] = csrfInput.value;

                    select.disabled = true;

                    fetch(quickUrl, {
                        method: 'POST',
                        headers: headers,
                        body: body
                    })
                    .then(function (response) {
                        return response.json().then(function (data) {
                            if (!response.ok) throw new Error(data.error || 'Could not change pipeline.');
                            return data;
                        });
                    })
                    .then(function (data) {
                        if (typeof window.shvyaToast === 'function') {
                            window.shvyaToast(
                                'Pipeline changed to ' + (data.pipeline_name || 'the selected pipeline') + '.',
                                'success',
                                {title: 'Pipeline updated'}
                            );
                        }
                        window.setTimeout(function () { window.location.reload(); }, 350);
                    })
                    .catch(function (error) {
                        select.disabled = false;
                        if (typeof window.shvyaToast === 'function') {
                            window.shvyaToast(error.message, 'error', {title: 'Pipeline update failed'});
                        }
                    });
                });
            })
            .catch(function (error) {
                changeLink.textContent = 'Change';
                if (typeof window.shvyaToast === 'function') {
                    window.shvyaToast(error.message, 'error', {title: 'Pipeline update failed'});
                }
            });
    });
})();
</script>
'''


class WhatsAppThemeMiddleware:
    """Inject the green channel theme only into server-rendered WhatsApp pages."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        if not request.path.startswith("/dashboard/whatsapp/"):
            return response

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

        lower_html = html.lower()

        if "</head>" in lower_html and "id=\"shvya-whatsapp-theme\"" not in html:
            index = lower_html.rfind("</head>")
            html = html[:index] + WHATSAPP_THEME + html[index:]

        if (
            request.path.startswith("/dashboard/whatsapp/chats/")
            and "</body>" in html.lower()
            and "id=\"shvya-whatsapp-pipeline-ui\"" not in html
        ):
            index = html.lower().rfind("</body>")
            html = html[:index] + WHATSAPP_CHAT_PIPELINE_UI + html[index:]

        response.content = html.encode(response.charset or "utf-8")
        if response.has_header("Content-Length"):
            response["Content-Length"] = str(len(response.content))
        return response
