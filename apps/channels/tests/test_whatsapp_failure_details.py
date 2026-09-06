import json
from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.channels.providers.whatsapp import WhatsAppAPIError
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization
from services.channels import whatsapp_service
from services.channels.whatsapp_error_service import describe_whatsapp_failure


class WhatsAppFailureDetailsTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Failure Details Org")
        self.user = User.objects.create_user(
            email="failure-details@example.com",
            password="test-password",
            name="Failure Details Admin",
            organization=self.org,
            role=User.Role.ADMIN,
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.org,
            business_name="Failure WABA",
            phone_number_id="123456789",
            waba_id="987654321",
            access_token="test-token",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        self.pipeline = Pipeline.objects.create(organization=self.org, name="Sales")
        self.stage = Stage.objects.create(pipeline=self.pipeline, name="New")
        self.lead = Lead.objects.create(
            organization=self.org,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Failure Lead",
            phone="+919000000001",
        )

        session = SessionStore()
        set_authenticated_user(session, self.user)
        session.save()
        self.client.cookies["shvya_crm_sessionid"] = session.session_key

    def _outbound(self, **kwargs):
        values = {
            "organization": self.org,
            "account": self.account,
            "lead": self.lead,
            "direction": WhatsAppMessage.Direction.OUTBOUND,
            "from_number": self.account.phone_number_id,
            "to_number": self.lead.phone,
            "body": "Hello",
            "status": WhatsAppMessage.Status.QUEUED,
        }
        values.update(kwargs)
        return WhatsAppMessage.objects.create(**values)

    def test_known_meta_code_returns_standard_code_why_resolve(self):
        details = describe_whatsapp_failure(
            raw_payload={
                "errors": [
                    {
                        "code": 131049,
                        "title": "This message was not delivered",
                        "error_data": {"details": "Message failed due to healthy ecosystem rules."},
                    }
                ]
            }
        )
        self.assertEqual(details["code"], "131049")
        self.assertEqual(details["title"], "Marketing Message Limited")
        self.assertIn("Healthy Ecosystem", details["why"])
        self.assertIn("Retry later", details["resolve"])

    def test_unknown_meta_code_keeps_exact_meta_detail(self):
        details = describe_whatsapp_failure(
            raw_payload={
                "errors": [
                    {
                        "code": 199999,
                        "title": "Custom Meta failure",
                        "error_data": {"details": "Exact Meta diagnostic text"},
                    }
                ]
            }
        )
        self.assertEqual(details["code"], "199999")
        self.assertEqual(details["title"], "Custom Meta failure")
        self.assertEqual(details["why"], "Exact Meta diagnostic text")

    def test_failed_status_webhook_persists_exact_error_code(self):
        message = self._outbound(
            external_id="wamid.failure.131026",
            status=WhatsAppMessage.Status.SENT,
        )
        whatsapp_service.handle_status_update(
            external_id=message.external_id,
            status="failed",
            raw_payload={
                "id": message.external_id,
                "status": "failed",
                "errors": [
                    {
                        "code": 131026,
                        "title": "Message undeliverable",
                        "error_data": {"details": "Unable to deliver message"},
                    }
                ],
            },
        )
        message.refresh_from_db()
        self.assertEqual(message.status, WhatsAppMessage.Status.FAILED)
        self.assertIn("131026", message.error)
        self.assertIn("Message Undeliverable", message.error)
        self.assertEqual(message.raw_payload["errors"][0]["code"], 131026)

    @patch("services.channels.whatsapp_service.WhatsAppClient.send_text_message")
    def test_immediate_api_failure_preserves_meta_response_code(self, send_text):
        send_text.side_effect = WhatsAppAPIError(
            "WhatsApp API returned 400",
            status_code=400,
            response_body=json.dumps(
                {
                    "error": {
                        "code": 131009,
                        "message": "Parameter value is not valid",
                        "error_data": {"details": "Recipient phone format is invalid"},
                    }
                }
            ),
        )
        message = self._outbound()

        with self.assertRaises(whatsapp_service.WhatsAppSendError):
            whatsapp_service.send_outbound_message(message=message)

        message.refresh_from_db()
        self.assertEqual(message.status, WhatsAppMessage.Status.FAILED)
        self.assertIn("131009", message.error)
        self.assertEqual(
            message.raw_payload["meta_error_response"]["error"]["code"],
            131009,
        )

    def test_chat_view_displays_code_why_and_resolve_for_failed_message(self):
        self._outbound(
            external_id="wamid.chat.failure",
            status=WhatsAppMessage.Status.FAILED,
            error="",
            raw_payload={
                "id": "wamid.chat.failure",
                "status": "failed",
                "errors": [
                    {
                        "code": 131049,
                        "title": "Message not delivered",
                        "error_data": {"details": "Healthy ecosystem limit applied"},
                    }
                ],
            },
        )

        response = self.client.get(reverse("whatsapp-chat-detail", args=[self.lead.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Not sent · View details")
        self.assertContains(response, "131049 — Marketing Message Limited")
        self.assertContains(response, "Why:")
        self.assertContains(response, "Healthy Ecosystem")
        self.assertContains(response, "Resolve:")
        self.assertContains(response, "Retry later")
