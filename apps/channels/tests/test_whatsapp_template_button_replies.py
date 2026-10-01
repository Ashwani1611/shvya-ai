from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization
from apps.channels.whatsapp_api_chat_ui import _attach_inbound_reply_display
from services.channels.whatsapp_service import (
    extract_inbound_message_body,
    handle_inbound_message,
)


class WhatsAppInboundReplyBodyTests(SimpleTestCase):
    def test_template_quick_reply_uses_visible_button_text(self):
        payload = {
            "type": "button",
            "button": {
                "payload": "not_interested",
                "text": "Not interested",
            },
        }
        self.assertEqual(
            extract_inbound_message_body(payload),
            "Not interested",
        )

    def test_interactive_button_reply_uses_visible_title(self):
        payload = {
            "type": "interactive",
            "interactive": {
                "type": "button_reply",
                "button_reply": {
                    "id": "rebook_demo",
                    "title": "Rebook the demo",
                },
            },
        }
        self.assertEqual(
            extract_inbound_message_body(payload),
            "Rebook the demo",
        )

    def test_interactive_list_reply_uses_visible_title(self):
        payload = {
            "type": "interactive",
            "interactive": {
                "type": "list_reply",
                "list_reply": {
                    "id": "slot_2",
                    "title": "Tomorrow at 2 PM",
                    "description": "Available slot",
                },
            },
        }
        self.assertEqual(
            extract_inbound_message_body(payload),
            "Tomorrow at 2 PM",
        )

    def test_historical_blank_reply_is_rendered_without_database_mutation(self):
        message = SimpleNamespace(
            direction=WhatsAppMessage.Direction.INBOUND,
            body="",
            raw_payload={
                "type": "button",
                "button": {"payload": "not_interested", "text": "Not interested"},
            },
        )
        _attach_inbound_reply_display([message])
        self.assertEqual(message.body, "Not interested")


class WhatsAppInboundReplyPersistenceTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Reply Buttons")
        self.pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Sales",
        )
        self.stage = (
            Stage.objects.filter(pipeline=self.pipeline)
            .order_by("display_order")
            .first()
            or Stage.objects.create(
                pipeline=self.pipeline,
                name="New",
                display_order=0,
            )
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="API",
            phone_number_id="123456789",
            display_phone_number="+919876543210",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Customer",
            phone="+918700274739",
        )

    @patch(
        "services.channels.hosted_whatsapp_service.get_session_settings",
        return_value={"auto_lead_creation": False, "ai_auto_reply": False},
    )
    @patch("services.channels.realtime.queue_message_publish")
    def test_template_button_reply_is_persisted_as_text(
        self,
        _publish,
        _settings,
    ):
        payload = {
            "id": "wamid.button.1",
            "from": "918700274739",
            "type": "button",
            "button": {
                "payload": "not_interested",
                "text": "Not interested",
            },
        }

        message = handle_inbound_message(
            organization=self.organization,
            account=self.account,
            external_id=payload["id"],
            from_number=payload["from"],
            to_number=self.account.display_phone_number,
            body="",
            raw_payload=payload,
        )

        self.assertEqual(message.body, "Not interested")
        self.assertEqual(message.message_type, WhatsAppMessage.MessageType.TEXT)

    def test_retry_repairs_legacy_blank_button_reply(self):
        payload = {
            "id": "wamid.button.retry",
            "from": "918700274739",
            "type": "button",
            "button": {
                "payload": "rebook_demo",
                "text": "Rebook the demo",
            },
        }
        legacy = WhatsAppMessage.objects.create(
            organization=self.organization,
            account=self.account,
            lead=self.lead,
            direction=WhatsAppMessage.Direction.INBOUND,
            external_id=payload["id"],
            from_number=payload["from"],
            to_number=self.account.display_phone_number,
            body="",
            message_type=WhatsAppMessage.MessageType.TEXT,
            status=WhatsAppMessage.Status.RECEIVED,
            raw_payload=payload,
        )

        returned = handle_inbound_message(
            organization=self.organization,
            account=self.account,
            external_id=payload["id"],
            from_number=payload["from"],
            to_number=self.account.display_phone_number,
            body="",
            raw_payload=payload,
        )

        legacy.refresh_from_db()
        self.assertEqual(returned.pk, legacy.pk)
        self.assertEqual(legacy.body, "Rebook the demo")
