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

        if "</head>" not in html.lower() or "id=\"shvya-whatsapp-theme\"" in html:
            return response

        index = html.lower().rfind("</head>")
        html = html[:index] + WHATSAPP_THEME + html[index:]
        response.content = html.encode(response.charset or "utf-8")
        if response.has_header("Content-Length"):
            response["Content-Length"] = str(len(response.content))
        return response
