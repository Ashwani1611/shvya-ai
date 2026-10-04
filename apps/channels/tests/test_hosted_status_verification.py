"""Sender eligibility and gateway availability must not be shown as Running."""
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import RequestFactory, SimpleTestCase

from apps.channels.hosted_ui import _reconcile_gateway_status, hosted_session_status_view
from apps.channels.models import WhatsAppAccount
from apps.channels.providers.whatsapp_web import WhatsAppWebGatewayError


class HostedStatusVerificationTests(SimpleTestCase):
    def account(self, status):
        return SimpleNamespace(id="account", status=status, display_phone_number="+918700274739",
                               refresh_from_db=Mock())

    @patch("apps.channels.hosted_ui.handle_gateway_event")
    def test_running_gateway_with_rejected_account_reports_effective_state(self, event):
        for persisted, expected in ((WhatsAppAccount.Status.FAILED, "failed"),
                                    (WhatsAppAccount.Status.PENDING, "connecting"),
                                    (WhatsAppAccount.Status.DISCONNECTED, "disconnected")):
            account = self.account(persisted)
            result = {"status": "running", "phoneNumber": "+918700274739"}
            with self.subTest(persisted=persisted):
                self.assertEqual(_reconcile_gateway_status(account, result), expected)
                self.assertEqual(result["status"], expected)
                self.assertIn("verification", result["lastError"])

    @patch("apps.channels.hosted_ui.handle_gateway_event")
    def test_wrong_linked_number_has_actionable_customer_safe_reason(self, event):
        result = {"status": "running", "phoneNumber": "+919876543210"}
        self.assertEqual(_reconcile_gateway_status(self.account(WhatsAppAccount.Status.FAILED), result), "failed")
        self.assertIn("does not match this account", result["lastError"])

    @patch("apps.channels.hosted_ui.handle_gateway_event")
    def test_verified_connected_sender_remains_running(self, event):
        result = {"status": "running", "phoneNumber": "+918700274739"}
        self.assertEqual(_reconcile_gateway_status(self.account(WhatsAppAccount.Status.CONNECTED), result), "running")
        self.assertNotIn("lastError", result)

    @patch("apps.channels.hosted_ui.gateway_client_for_account")
    @patch("apps.channels.hosted_ui._hosted_account")
    def test_gateway_unavailability_cannot_return_cached_running_label(self, account_lookup, gateway):
        account = self.account(WhatsAppAccount.Status.CONNECTED)
        account_lookup.return_value = account
        gateway.return_value.get_session.side_effect = WhatsAppWebGatewayError("Private internal address", status_code=503)
        response = hosted_session_status_view.__wrapped__.__wrapped__(RequestFactory().get("/"), "account")
        payload = json.loads(response.content)
        self.assertEqual(response.status_code, 503)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "unavailable")
        self.assertEqual(payload["label"], "Gateway unavailable")
        self.assertEqual(account.status, WhatsAppAccount.Status.CONNECTED)
        self.assertNotIn("Private internal", payload["error"])
