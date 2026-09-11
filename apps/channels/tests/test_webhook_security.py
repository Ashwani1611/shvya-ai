import hashlib
import hmac

from django.test import SimpleTestCase, override_settings
from django.urls import reverse


class WhatsAppWebhookSecurityTests(SimpleTestCase):
    @override_settings(META_VERIFY_TOKEN="verify-me")
    def test_verification_accepts_matching_token(self):
        response = self.client.get(
            reverse("whatsapp-webhook"),
            {
                "hub.mode": "subscribe",
                "hub.verify_token": "verify-me",
                "hub.challenge": "challenge-123",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"challenge-123")

    @override_settings(META_VERIFY_TOKEN="verify-me")
    def test_verification_rejects_wrong_token(self):
        response = self.client.get(
            reverse("whatsapp-webhook"),
            {
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong-token",
                "hub.challenge": "challenge-123",
            },
        )

        self.assertEqual(response.status_code, 403)

    @override_settings(META_APP_SECRET="")
    def test_delivery_fails_closed_without_app_secret(self):
        response = self.client.post(
            reverse("whatsapp-webhook"),
            data="{}",
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)

    @override_settings(META_APP_SECRET="meta-secret")
    def test_delivery_rejects_invalid_signature(self):
        response = self.client.post(
            reverse("whatsapp-webhook"),
            data="{}",
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256="sha256=invalid",
        )

        self.assertEqual(response.status_code, 403)

    @override_settings(META_APP_SECRET="meta-secret")
    def test_delivery_accepts_valid_signature(self):
        body = b"{}"
        signature = hmac.new(
            b"meta-secret",
            body,
            hashlib.sha256,
        ).hexdigest()

        response = self.client.post(
            reverse("whatsapp-webhook"),
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=f"sha256={signature}",
        )

        self.assertEqual(response.status_code, 200)
