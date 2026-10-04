"""Inbound attachment projection, provider privacy, and dashboard access."""

import io
import uuid
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from apps.channels.meta_inbound_media import meta_inbound_media
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.channels.providers.whatsapp import GRAPH_API_BASE, WhatsAppAPIError, WhatsAppClient
from apps.channels.tests import test_chat_inbox as inbox
from apps.channels.whatsapp_api_chat_ui import _attach_inbound_reply_display
from apps.organizations.models import Organization
from services.channels.whatsapp_service import handle_inbound_message, inbound_message_supports_ai


def media_event(media_type="document", **values):
    payload = {"id": "123456789", "mime_type": "application/pdf", "filename": "Brochure.pdf"}
    if media_type == "image":
        payload = {"id": "123456789", "mime_type": "image/png"}
    payload.update(values)
    return {"type": media_type, media_type: payload}


class MetaInboundMediaProjectionTests(SimpleTestCase):
    def test_document_metadata_and_caption_preserve_the_received_attachment(self):
        event = media_event(caption="DIY costs 2999", url="https://private.example/token", access_token="secret")
        media_type, payload = meta_inbound_media(event)
        self.assertEqual(media_type, "document")
        self.assertEqual(payload, {"source": "meta_inbound", "media_id": "123456789",
                                   "mime_type": "application/pdf", "filename": "Brochure.pdf"})
        message = SimpleNamespace(pk=uuid.uuid4(), account_id=uuid.uuid4(), direction="inbound",
                                  body="DIY costs 2999", raw_payload=event, message_type="text", media_payload={})
        _attach_inbound_reply_display([message])
        html = render_to_string("channels/_whatsapp_inbound_attachment.html",
                                {"attachment": message.inbound_attachment, "body": message.body})
        self.assertIn("Brochure.pdf", html)
        self.assertIn("DIY costs 2999", html)
        self.assertIn("?download=1", html)
        self.assertNotIn("123456789", html)
        self.assertNotIn("private.example", html)
        self.assertEqual(message.message_type, "text")  # Historical projection is read-only.
        self.assertEqual(message.media_payload, {})

    def test_captionless_image_renders_preview_and_keeps_existing_ai_eligibility(self):
        event = media_event("image")
        message = SimpleNamespace(pk=uuid.uuid4(), account_id=uuid.uuid4(), direction="inbound",
                                  body="", raw_payload=event)
        _attach_inbound_reply_display([message])
        self.assertIsNone(message.inbound_details)
        html = render_to_string("channels/_whatsapp_inbound_attachment.html",
                                {"attachment": message.inbound_attachment, "body": ""})
        self.assertIn('<img src="/dashboard/whatsapp/chats/account/', html)
        self.assertIn('loading="lazy"', html)
        self.assertNotIn("Unsupported", html)
        self.assertTrue(inbound_message_supports_ai(body="", raw_payload=event))
        self.assertFalse(inbound_message_supports_ai(body="", raw_payload={"type": "unsupported"}))

    def test_malformed_metadata_never_creates_an_arbitrary_download(self):
        for raw in (None, [], {"type": ["document"]}, {"type": "text", "document": {"id": "1"}},
                    media_event(id="https://internal.example"), media_event(id=["1"]),
                    media_event(id="1" * 65), media_event(id="１２３")):
            with self.subTest(raw=raw):
                _, payload = meta_inbound_media(raw)
                self.assertNotIn("media_id", payload)
        _, payload = meta_inbound_media(media_event(filename="../../folder\\Report\r\nX-Foo: value.pdf",
                                                    mime_type="text/html\r\nX-Foo: value"))
        self.assertEqual(payload["filename"], "ReportX-Foo: value.pdf")
        self.assertNotIn("mime_type", payload)

    def test_missing_media_id_shows_attachment_unavailable_without_provider_notice(self):
        message = SimpleNamespace(pk=uuid.uuid4(), account_id=uuid.uuid4(), direction="inbound",
                                  body="", raw_payload=media_event(id=""))
        _attach_inbound_reply_display([message])
        self.assertEqual(message.inbound_attachment["url"], "")
        self.assertIsNone(message.inbound_details)
        html = render_to_string("channels/_whatsapp_inbound_attachment.html",
                                {"attachment": message.inbound_attachment})
        self.assertIn("Document attachment unavailable", html)
        self.assertNotIn("href=", html)


class MetaMediaProviderTests(SimpleTestCase):
    def setUp(self):
        self.client = WhatsAppClient("receiving-phone", "local-test-only-token")

    def response(self, *, status=200, metadata=None, chunks=(b"%PDF-test",), headers=None):
        response = Mock(status_code=status, headers=headers or {})
        response.json.return_value = metadata or {
            "url": "https://lookaside.fbsbx.com/whatsapp_business/attachments/?signed=private",
            "mime_type": "application/pdf", "file_size": 9,
        }
        response.iter_content.return_value = iter(chunks)
        return response

    @patch("apps.channels.providers.whatsapp.requests.get")
    def test_media_lookup_is_phone_bound_and_authenticated_without_redirects(self, get):
        metadata, download = self.response(), self.response()
        get.side_effect = [metadata, download]
        result = self.client.download_media("123456789")
        try:
            self.assertEqual(result["file"].read(), b"%PDF-test")
            self.assertEqual(result["mime_type"], "application/pdf")
            self.assertEqual(get.call_args_list[0].args, (f"{GRAPH_API_BASE}/123456789",))
            self.assertEqual(get.call_args_list[0].kwargs["params"], {"phone_number_id": "receiving-phone"})
            for call in get.call_args_list:
                self.assertEqual(call.kwargs["headers"], {"Authorization": "Bearer local-test-only-token"})
                self.assertFalse(call.kwargs["allow_redirects"])
                self.assertEqual(call.kwargs["timeout"], (5, 15))
            metadata.close.assert_called_once()
            download.close.assert_called_once()
        finally:
            result["file"].close()

    @patch("apps.channels.providers.whatsapp.requests.get")
    def test_unsafe_urls_and_metadata_never_receive_credentials(self, get):
        for url in ("http://lookaside.fbsbx.com/file", "https://localhost/file", "https://169.254.169.254/",
                    "https://lookaside.fbsbx.com.evil.example/file", "https://user:pass@lookaside.fbsbx.com/file",
                    "https://lookaside.fbsbx.com:444/file", "https://lookaside.fbsbx.com/file#fragment",
                    "https://lookaside.fbsbx.com:bad/file", "https://[invalid/file"):
            with self.subTest(url=url):
                get.reset_mock()
                get.side_effect = None
                get.return_value = self.response(metadata={"url": url})
                with self.assertRaises(WhatsAppAPIError) as error:
                    self.client.download_media("123456789")
                self.assertNotIn(url, str(error.exception))
                self.assertEqual(get.call_count, 1)

    @patch("apps.channels.providers.whatsapp.requests.get")
    def test_redirect_provider_error_and_network_error_do_not_leak_details(self, get):
        for failed_step in (0, 1):
            for status in (302, 401, 404, 410, 500):
                with self.subTest(step=failed_step, status=status):
                    failed = self.response(status=status)
                    get.side_effect = [failed] if failed_step == 0 else [self.response(), failed]
                    with self.assertRaises(WhatsAppAPIError) as error:
                        self.client.download_media("123456789")
                    self.assertEqual(error.exception.status_code, status)
                    self.assertEqual(str(error.exception), "Media is unavailable.")
                    self.assertIsNone(error.exception.response_body)
                    failed.close.assert_called_once()
        get.side_effect = requests.RequestException("signed=private local-test-only-token")
        with self.assertRaises(WhatsAppAPIError) as error:
            self.client.download_media("123456789")
        self.assertEqual(str(error.exception), "Media could not be downloaded.")

    @patch("apps.channels.providers.whatsapp.requests.get")
    def test_declared_and_streaming_sizes_are_bounded_even_without_content_length(self, get):
        scenarios = [
            (self.response(metadata={"url": "https://lookaside.fbsbx.com/file", "file_size": 11}), None),
            (self.response(metadata={"url": "https://lookaside.fbsbx.com/file"}), self.response(headers={"Content-Length": "11"})),
            (self.response(metadata={"url": "https://lookaside.fbsbx.com/file"}), self.response(chunks=(b"123456", b"123456"))),
        ]
        for metadata, download in scenarios:
            with self.subTest(download=bool(download)):
                get.side_effect = [metadata, download] if download is not None else [metadata]
                with self.assertRaises(WhatsAppAPIError) as error:
                    self.client.download_media("123456789", max_bytes=10)
                self.assertEqual(error.exception.status_code, 413)
                if download is not None:
                    download.close.assert_called_once()

    @patch("apps.channels.providers.whatsapp.requests.get")
    def test_invalid_media_ids_and_missing_credentials_do_not_call_provider(self, get):
        for media_id in ("", "../private", "https://example.com", "１２３", "1" * 65, 123):
            with self.subTest(media_id=media_id), self.assertRaises(WhatsAppAPIError):
                self.client.download_media(media_id)
        with self.assertRaises(WhatsAppAPIError):
            WhatsAppClient("", "").download_media("1")
        get.assert_not_called()


class MetaInboundMediaScopeTests(TestCase):
    setUp = inbox.WhatsAppChatInboxTests.setUp
    make_lead = inbox.WhatsAppChatInboxTests.make_lead
    make_message = inbox.WhatsAppChatInboxTests.make_message

    def media_url(self, message, account=None):
        return reverse("whatsapp-api-chat-media", args=[(account or self.account).pk, message.pk])

    @patch("apps.channels.providers.whatsapp.WhatsAppClient.download_media")
    def test_download_is_bound_to_stored_message_and_exact_receiving_account(self, download):
        self.account.access_token = "local-test-only-token"
        self.account.save(update_fields=["access_token"])
        lead = self.make_lead("Document lead", "+919000000110")
        message = self.make_message(lead, body="2999", raw_payload=media_event(), media_payload={"media_id": "wrong"})
        download.return_value = {"file": io.BytesIO(b"%PDF-test"), "mime_type": "application/pdf"}
        response = self.client.get(self.media_url(message), {"media_id": "client-controlled", "url": "https://evil.example"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"%PDF-test")
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(response["Content-Disposition"], 'attachment; filename="Brochure.pdf"')
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("sandbox", response["Content-Security-Policy"])
        download.assert_called_once_with("123456789", max_bytes=100 * 1024 * 1024)
        response.close()

    @patch("apps.channels.providers.whatsapp.WhatsAppClient.download_media")
    def test_foreign_org_account_hosted_outbound_and_invalid_media_are_rejected(self, download):
        lead = self.make_lead("Document lead", "+919000000111")
        own = self.make_message(lead, body="", raw_payload=media_event())
        wrong = WhatsAppAccount.objects.create(organization=self.org, phone_number_id="media-other", status="connected")
        foreign_org = Organization.objects.create(name="Foreign media", package="dfy")
        foreign = WhatsAppAccount.objects.create(organization=foreign_org, phone_number_id="media-private", status="connected")
        hosted = WhatsAppAccount.objects.create(organization=self.org, phone_number_id="media-hosted", connection_type="hosted", status="connected")
        for account in (wrong, foreign, hosted):
            with self.subTest(account=account.pk):
                self.assertEqual(self.client.get(self.media_url(own, account)).status_code, 404)
        for values in ({"direction": "outbound", "raw_payload": media_event()},
                       {"raw_payload": {"type": "unsupported"}}, {"raw_payload": media_event(id="")},
                       {"organization": foreign_org, "account": foreign, "raw_payload": media_event()}):
            message = self.make_message(lead, **values)
            self.assertEqual(self.client.get(self.media_url(message)).status_code, 404)
        download.assert_not_called()

    @patch("apps.channels.providers.whatsapp.WhatsAppClient.download_media")
    def test_unlinked_download_requires_dashboard_auth_and_keeps_no_lead(self, download):
        message = WhatsAppMessage.objects.create(organization=self.org, account=self.account, direction="inbound",
                                                status="received", body="", raw_payload=media_event())
        download.return_value = {"file": io.BytesIO(b"%PDF-test"), "mime_type": "application/pdf"}
        response = self.client.get(self.media_url(message))
        self.assertEqual(response.status_code, 200)
        response.close()
        message.refresh_from_db()
        self.assertIsNone(message.lead_id)
        self.client.cookies.clear()
        download.reset_mock()
        response = self.client.get(self.media_url(message))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/dashboard/login/", response.url)
        download.assert_not_called()

    @patch("apps.channels.providers.whatsapp.WhatsAppClient.download_media")
    def test_safe_image_inline_unsafe_types_download_and_filename_cannot_set_headers(self, download):
        lead = self.make_lead("Image lead", "+919000000112")
        message = self.make_message(lead, body="", raw_payload=media_event("image", filename='../../evil\r\nX-Test: yes.png'))
        for mime_type, disposition, expected_type in (
            ("image/png", "inline", "image/png"), ("image/svg+xml", "attachment", "application/octet-stream"),
            ("text/html", "attachment", "application/octet-stream"), (None, "attachment", "application/octet-stream"),
            (["image/png"], "attachment", "application/octet-stream"), ({"mime_type": "image/png"}, "attachment", "application/octet-stream"),
        ):
            with self.subTest(mime_type=mime_type):
                download.return_value = {"file": io.BytesIO(b"file"), "mime_type": mime_type}
                response = self.client.get(self.media_url(message))
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response["Content-Disposition"].startswith(disposition))
                self.assertEqual(response["Content-Type"], expected_type)
                self.assertNotIn("X-Test", response.headers)
                self.assertNotIn("\r", response["Content-Disposition"])
                response.close()
        download.return_value = {"file": io.BytesIO(b"file"), "mime_type": "image/png"}
        response = self.client.get(self.media_url(message), {"download": "1"})
        self.assertTrue(response["Content-Disposition"].startswith("attachment"))
        response.close()

    @patch("apps.channels.providers.whatsapp.WhatsAppClient.download_media")
    def test_provider_errors_are_fixed_private_responses(self, download):
        lead = self.make_lead("Document lead", "+919000000113")
        message = self.make_message(lead, raw_payload=media_event())
        for status, expected in ((404, 404), (410, 404), (413, 413), (401, 502), (500, 502), (None, 502)):
            with self.subTest(status=status):
                download.side_effect = WhatsAppAPIError("token secret https://signed.example/private", status_code=status,
                                                       response_body="PRIVATE PROVIDER BODY")
                response = self.client.get(self.media_url(message))
                self.assertEqual(response.status_code, expected)
                self.assertEqual(response.json(), {"error": "This attachment is currently unavailable."})
                self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_historical_captioned_document_and_captionless_image_render_without_rewriting(self):
        lead = self.make_lead("Media lead", "+919000000114")
        document = self.make_message(lead, body="2999", raw_payload=media_event())
        image = self.make_message(lead, body="", raw_payload=media_event("image"))
        response = self.client.get(reverse("whatsapp-chat-detail", args=[lead.pk]), {"account": self.account.pk})
        self.assertContains(response, self.media_url(document))
        self.assertContains(response, self.media_url(image))
        self.assertContains(response, "Brochure.pdf")
        self.assertContains(response, "2999")
        self.assertNotContains(response, "Unsupported message type")
        for message in (document, image):
            message.refresh_from_db()
            self.assertEqual(message.message_type, "text")
            self.assertEqual(message.media_payload, {})

    @patch("services.channels.whatsapp_service._queue_internal_conversation_summary")
    @patch("services.channels.whatsapp_service._queue_whatsapp_engagement")
    @patch("services.channels.realtime.queue_message_publish")
    def test_new_media_ingestion_preserves_type_caption_metadata_and_idempotency(self, publish, engagement, summary):
        for index, media_type in enumerate(("document", "image")):
            event = media_event(media_type, caption="Customer caption")
            values = dict(organization=self.org, account=self.account, external_id=f"wamid.media.{index}",
                          from_number=f"91900000012{index}", to_number="919000000000", body="", raw_payload=event)
            message = handle_inbound_message(**values)
            self.assertEqual(message.message_type, media_type)
            self.assertEqual(message.body, "Customer caption")
            self.assertEqual(message.media_payload["media_id"], "123456789")
            self.assertEqual(message.raw_payload, event)
            self.assertEqual(handle_inbound_message(**values).pk, message.pk)
            self.assertEqual(WhatsAppMessage.objects.filter(external_id=values["external_id"]).count(), 1)
        self.assertEqual(publish.call_count, 2)
