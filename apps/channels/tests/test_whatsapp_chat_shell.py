from unittest.mock import patch

from django.http import HttpResponse
from django.test import SimpleTestCase

from apps.channels import whatsapp_chat_failure_ui


class WhatsAppChatShellTests(SimpleTestCase):
    def _html_response(self):
        return HttpResponse(
            "<html><head></head><body>"
            "<div><aside></aside><main>"
            "<div id='thread' class='wa-chat-surface'></div>"
            "</main><aside></aside></div>"
            "</body></html>"
        )

    def test_chat_ui_injects_locked_shell_and_whatsapp_surface(self):
        response = whatsapp_chat_failure_ui._inject_chat_ui(self._html_response())
        html = response.content.decode("utf-8")

        self.assertIn("data-shvya-whatsapp-web-shell", html)
        self.assertIn("data-shvya-whatsapp-chat-ui", html)
        self.assertIn(".wa-page-host", html)
        self.assertIn("overflow: hidden !important", html)
        self.assertIn("overscroll-behavior: contain", html)
        self.assertIn("background-color: #efeae2", html)
        self.assertIn("wa-conversation-pane", html)
        self.assertIn("wa-context-pane", html)
        self.assertIn("wa-mobile-back", html)

    def test_chat_ui_is_idempotent(self):
        response = whatsapp_chat_failure_ui._inject_chat_ui(self._html_response())
        response = whatsapp_chat_failure_ui._inject_chat_ui(response)
        html = response.content.decode("utf-8")

        self.assertEqual(html.count("data-shvya-whatsapp-web-shell"), 1)
        self.assertEqual(html.count("data-shvya-whatsapp-chat-ui"), 1)

    @patch("apps.channels.whatsapp_chat_failure_ui.views_flat.whatsapp_chat_list_view")
    def test_list_route_uses_same_locked_shell(self, list_view):
        list_view.return_value = self._html_response()

        response = whatsapp_chat_failure_ui.whatsapp_chat_list_view(object())
        html = response.content.decode("utf-8")

        list_view.assert_called_once()
        self.assertIn("data-shvya-whatsapp-web-shell", html)
        self.assertIn("wa-empty-chat", html)

    @patch("apps.channels.whatsapp_chat_failure_ui.views_flat.whatsapp_chat_detail_view")
    def test_detail_route_keeps_failure_summary_enrichment(self, detail_view):
        detail_view.return_value = HttpResponse(
            "<html><head></head><body>"
            "<main><div id='thread' class='wa-chat-surface'>"
            "<details><summary>Not sent · View details</summary>"
            "<div class='font-semibold text-red-700'>131049 — Marketing Message Limited</div>"
            "</details></div></main>"
            "</body></html>"
        )

        response = whatsapp_chat_failure_ui.whatsapp_chat_detail_view(object(), "lead-id")
        html = response.content.decode("utf-8")

        detail_view.assert_called_once_with(object(), "lead-id")
        self.assertIn("Not sent \\u00b7", html)
        self.assertIn("data-shvya-whatsapp-chat-ui", html)
