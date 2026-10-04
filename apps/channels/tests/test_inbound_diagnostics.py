"""Readable, tenant-scoped diagnostics without leaking provider payloads."""

import json
import uuid
from types import SimpleNamespace

from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from apps.channels.inbound_diagnostics import inbound_event_details
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.channels.tests import test_chat_inbox as inbox
from apps.channels.whatsapp_api_chat_ui import _attach_inbound_reply_display
from apps.crm.models import Lead, Pipeline
from apps.organizations.models import Organization


def unsupported_payload(code=131051):
    return {
        "type": "unsupported", "unsupported": {"type": "edit"},
        "errors": [{"code": code, "title": "Message type unknown",
                    "error_data": {"details": "Message type is currently not supported."}}],
    }


class InboundDiagnosticProjectionTests(SimpleTestCase):
    def message(self, payload, **kwargs):
        return SimpleNamespace(pk=uuid.uuid4(), direction="inbound", body="", raw_payload=payload, **kwargs)

    def test_meta_error_is_visible_without_payload_or_message_content(self):
        payload = unsupported_payload()
        payload.update({"from": "919876543210", "text": {"body": "PRIVATE CUSTOMER TEXT"},
                        "access_token": "PRIVATE PROVIDER TOKEN", "contacts": [{"name": "PRIVATE NAME"}]})
        message = self.message(payload)
        details = inbound_event_details(message)
        self.assertEqual(details, {
            "message_id": str(message.pk), "provider_type": "unsupported", "unsupported_type": "edit",
            "errors": [{"code": "131051", "title": "Message type unknown",
                        "detail": "Message type is currently not supported."}],
        })
        html = render_to_string("channels/_whatsapp_inbound_details.html", {"details": details})
        self.assertIn("No readable message text was included in the received event.", html)
        self.assertIn("131051", html)
        for hidden in ("PRIVATE CUSTOMER TEXT", "PRIVATE PROVIDER TOKEN", "PRIVATE NAME", "919876543210"):
            self.assertNotIn(hidden, html)

    def test_malformed_fields_are_ignored_without_stringifying_nested_values(self):
        payloads = [None, [], "raw secret", {"type": ["text"], "unsupported": ["edit"], "errors": {}},
                    {"type": "<script>", "unsupported": {"type": "call +919876543210"},
                     "errors": [False, {"code": True, "title": {"token": "nested secret"}},
                                {"code": [131051], "error_data": {"details": {"body": "private body"}}}]}]
        for payload in payloads:
            with self.subTest(payload=payload):
                details = inbound_event_details(self.message(payload))
                self.assertEqual(details["provider_type"], "Unavailable")
                self.assertEqual(details["unsupported_type"], "")
                self.assertEqual(details["errors"], [])

    def test_legacy_unknown_event_and_unavailable_error_keep_provider_evidence(self):
        for payload in (
            {"type": "unknown", "errors": [{"code": 131051, "title": "Unsupported message type",
                                          "details": "Message type is not currently supported"}]},
            {"type": "unsupported", "errors": [{"code": 131060, "title": "This message is currently unavailable",
                                              "message": "This message is currently unavailable"}]},
        ):
            with self.subTest(provider_type=payload["type"]):
                details = inbound_event_details(self.message(payload))
                self.assertEqual(details["provider_type"], payload["type"])
                self.assertEqual(details["errors"][0]["code"], str(payload["errors"][0]["code"]))
                self.assertTrue(details["errors"][0]["detail"])

    def test_sensitive_error_values_are_redacted_and_html_escaped(self):
        private_values = ["local-test-api-secret", "local-test-password", "abcdefghijklmnop1234567890",
                          "person@example.com", "+91 98765 43210", "919876543210",
                          "https://example.com/private?token=local-test-link-secret",
                          "quoted-secret", "short-bearer", "dXNlcjpwYXNz", "csrf-cookie-secret"]
        payload = unsupported_payload()
        payload["errors"][0].update({
            "title": "<script>alert('x')</script>",
            "error_data": {"details": "api_key=local-test-api-secret password=local-test-password "
                           "Bearer abcdefghijklmnop1234567890 person@example.com +91 98765 43210 "
                           '919876543210 https://example.com/private?token=local-test-link-secret '
                           '"access_token": "quoted-secret" Bearer short-bearer '
                           'Authorization: Basic dXNlcjpwYXNz Authorization: Bearer short-bearer '
                           'Cookie: sessionid=short-cookie; csrftoken=csrf-cookie-secret'},
        })
        details = inbound_event_details(self.message(payload))
        serialized = json.dumps(details)
        for private in private_values:
            self.assertNotIn(private, serialized)
        html = render_to_string("channels/_whatsapp_inbound_details.html", {"details": details})
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("[contact omitted]", html)

    def test_errors_and_strings_are_bounded(self):
        payload = unsupported_payload()
        payload["errors"] = [{"code": 131051, "title": "a " * 5000, "details": "b " * 5000}] * 4
        details = inbound_event_details(self.message(payload))
        self.assertEqual(len(details["errors"]), 3)
        for error in details["errors"]:
            self.assertLessEqual(len(error["title"]), 120)
            self.assertLessEqual(len(error["detail"]), 400)

    def test_oversized_metadata_cannot_leak_partial_contacts_or_secrets(self):
        for value in ('password="' + 'short secret ' * 500 + '"',
                      "person" * 820 + "@example.com", "+91 " + "9 " * 2050):
            with self.subTest(kind=value[:10]):
                payload = unsupported_payload()
                payload["errors"][0]["error_data"]["details"] = value
                details = inbound_event_details(self.message(payload))
                self.assertEqual(details["errors"][0]["detail"], "[provider text omitted: too long]")
        payload = unsupported_payload()
        payload["errors"][0]["code"] = 10 ** 5000
        self.assertEqual(inbound_event_details(self.message(payload))["errors"][0]["code"], "")

    def test_provider_identifiers_cannot_carry_credentials_or_contact_numbers(self):
        for value in ("shvya_localtesttoken1234567890", "customer919876543210", "unknown_secret_identifier"):
            with self.subTest(identifier=value):
                payload = unsupported_payload()
                payload["type"] = value
                payload["unsupported"]["type"] = value
                payload["errors"][0]["code"] = 919876543210
                details = inbound_event_details(self.message(payload))
                self.assertEqual(details["provider_type"], "Unavailable")
                self.assertEqual(details["unsupported_type"], "")
                self.assertEqual(details["errors"][0]["code"], "")
                self.assertNotIn(value, json.dumps(details))

    def test_supported_text_and_recovered_button_reply_remain_unchanged(self):
        text = self.message({"type": "text", "text": {"body": "Original customer message"}})
        text.body = "Original customer message"
        legacy_text = self.message({"type": "text", "text": {"body": "Recovered customer message"}})
        button = self.message({"type": "button", "button": {"text": "Interested"}})
        outbound = self.message(unsupported_payload())
        outbound.direction = "outbound"
        _attach_inbound_reply_display([text, legacy_text, button, outbound])
        self.assertEqual([text.body, legacy_text.body, button.body],
                         ["Original customer message", "Recovered customer message", "Interested"])
        for message in (text, legacy_text, button, outbound):
            self.assertIsNone(message.inbound_details)
        self.assertEqual(legacy_text.raw_payload["text"]["body"], "Recovered customer message")


class InboundDiagnosticScopeTests(TestCase):
    setUp = inbox.WhatsAppChatInboxTests.setUp
    make_lead = inbox.WhatsAppChatInboxTests.make_lead
    make_message = inbox.WhatsAppChatInboxTests.make_message

    def test_linked_chat_details_follow_selected_account_and_organization(self):
        lead = self.make_lead("Diagnostic Lead", "+919000000100")
        own = self.make_message(lead, body="", raw_payload=unsupported_payload())
        second = WhatsAppAccount.objects.create(
            organization=self.org, phone_number_id="diagnostic-2", status="connected",
        )
        other_account_message = self.make_message(lead, account=second, body="", raw_payload=unsupported_payload(131060))
        other_org = Organization.objects.create(package="dfy", name="Private Org")
        other_account = WhatsAppAccount.objects.create(organization=other_org, phone_number_id="private", status="connected")
        other_pipeline = Pipeline.objects.create(organization=other_org, name="Private Sales")
        other_lead = Lead.objects.create(organization=other_org, pipeline=other_pipeline,
                                        stage=other_pipeline.stages.first(), name="Private lead", phone="+919000000200")
        private_message = WhatsAppMessage.objects.create(
            organization=other_org, account=other_account, lead=other_lead, direction="inbound", body="",
            raw_payload=unsupported_payload(131999), status="received",
        )
        url = reverse("whatsapp-chat-detail", args=[lead.pk])
        response = self.client.get(url, {"account": self.account.pk})
        self.assertContains(response, str(own.pk))
        self.assertContains(response, "Provider error code:</span> 131051")
        self.assertNotContains(response, str(other_account_message.pk))
        self.assertNotContains(response, str(private_message.pk))
        self.assertNotContains(response, "131060")
        self.assertNotContains(response, "131999")
        own.refresh_from_db()
        self.assertEqual(own.body, "")
        self.assertEqual(own.raw_payload, unsupported_payload())
        self.assertEqual(self.client.get(url, {"account": other_account.pk}).status_code, 404)
        foreign_response = self.client.get(reverse("whatsapp-chat-detail", args=[other_lead.pk]))
        self.assertEqual(foreign_response.status_code, 302)

    def test_unlinked_chat_has_scoped_details_without_creating_lead(self):
        own = WhatsAppMessage.objects.create(
            organization=self.org, account=self.account, direction="inbound", body="", status="received",
            from_number="919000000300", to_number="919000000400", raw_payload=unsupported_payload(),
        )
        url = reverse("whatsapp-unlinked-chat", args=[self.account.pk, own.pk])
        count = Lead.objects.count()
        response = self.client.get(url)
        self.assertContains(response, str(own.pk))
        self.assertContains(response, "Provider error code:</span> 131051")
        self.assertEqual(Lead.objects.count(), count)
        other_account = WhatsAppAccount.objects.create(organization=self.org, phone_number_id="unlinked-other", status="connected")
        wrong_account_response = self.client.get(reverse("whatsapp-unlinked-chat", args=[other_account.pk, own.pk]))
        self.assertEqual(wrong_account_response.status_code, 404)

    def test_diagnostics_require_existing_dashboard_authentication(self):
        lead = self.make_lead("Diagnostic Lead", "+919000000100")
        self.make_message(lead, body="", raw_payload=unsupported_payload())
        self.client.cookies.clear()
        response = self.client.get(reverse("whatsapp-chat-detail", args=[lead.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/dashboard/login/", response.url)
