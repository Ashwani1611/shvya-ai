from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from apps.channels.models import WhatsAppAccount
from apps.crm.models import Pipeline
from apps.organizations.models import Organization
from services.channels.hosted_whatsapp_service import handle_gateway_event


class HostedAISingleEnqueuePathTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Hosted Single Enqueue Org",
            settings={"hosted_account_enabled": True},
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Sales",
            country_code="+91",
            phone_number="9999999999",
            is_active=True,
            ai_enabled=True,
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            business_name="Sales",
            display_phone_number="+919999999999",
            phone_number_id="+919999999999",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        settings = dict(self.organization.settings or {})
        settings["hosted_whatsapp"] = {
            "sessions": {
                str(self.account.id): {
                    "auto_lead_creation": True,
                    "ai_auto_reply": True,
                }
            }
        }
        self.organization.settings = settings
        self.organization.save(update_fields=["settings", "updated_at"])
        self.account.organization = self.organization

    def test_live_inbound_enqueues_once_from_committed_message_signal(self):
        payload = {
            "sessionId": str(self.account.id),
            "event": "message",
            "messageId": "single-enqueue-1",
            "from": "919876543210@c.us",
            "to": "919999999999@c.us",
            "fromMe": False,
            "body": "Hello",
            "messageType": "text",
            "chatId": "919876543210@c.us",
            "contactName": "Lead",
            "isGroup": False,
        }

        with (
            patch(
                "apps.ai_engagement.services.ai_permissions.AIPermissionService.evaluate",
                return_value=SimpleNamespace(allowed=True, reason="allowed"),
            ),
            patch(
                "services.channels.hosted_automation_service.enqueue_ai_engagement"
            ) as enqueue,
            self.captureOnCommitCallbacks(execute=True),
        ):
            message = handle_gateway_event(payload=payload)

        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.kwargs["account"], self.account)
        self.assertEqual(enqueue.call_args.kwargs["source_message"], message)
        self.assertEqual(enqueue.call_args.kwargs["lead"], message.lead)
