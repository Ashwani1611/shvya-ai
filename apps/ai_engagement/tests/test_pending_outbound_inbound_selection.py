"""Queued AI output cannot consume an unanswered customer turn."""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from apps.ai_engagement.services.engagement_execution import _latest_whatsapp_message
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline
from apps.organizations.models import Organization


class PendingOutboundInboundSelectionTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Queue Selection")
        self.pipeline = Pipeline.objects.create(
            organization=self.organization, name="Sales", country_code="+91", phone_number="9876543210",
        )
        self.lead = Lead.objects.create(
            organization=self.organization, pipeline=self.pipeline,
            stage=self.pipeline.stages.get(name="Qualified"),
            name="Customer", phone="+919111111111",
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization, connection_type="api", phone_number_id="queue-api",
            display_phone_number="+919876543210", status="connected", is_active=True,
        )
        self.inbound = WhatsAppMessage.objects.create(
            organization=self.organization, account=self.account, lead=self.lead,
            direction="inbound", status="received", body="Can you explain the next steps?",
            from_number=self.lead.phone, to_number=self.account.display_phone_number,
        )

    def _outbound(self, *, status="queued", payload=None, account=None):
        return WhatsAppMessage.objects.create(
            organization=self.organization, account=account or self.account, lead=self.lead,
            direction="outbound", status=status, body="Message",
            from_number=self.account.display_phone_number, to_number=self.lead.phone,
            raw_payload=payload or {},
        )

    def test_queued_welcome_does_not_hide_qualified_customer_message(self):
        self._outbound(payload={"shvya_welcome": {"trigger": "lead_created"}})
        self.assertEqual(_latest_whatsapp_message(lead=self.lead), self.inbound)

    def test_queued_or_failed_human_draft_does_not_consume_customer_turn(self):
        self._outbound(status="queued")
        self._outbound(status="failed")
        self.assertEqual(_latest_whatsapp_message(lead=self.lead), self.inbound)

    def test_successful_welcome_does_not_consume_customer_turn(self):
        self._outbound(status="sent", payload={"shvya_welcome": {"trigger": "lead_created"}})
        self.assertEqual(_latest_whatsapp_message(lead=self.lead), self.inbound)

    def test_successful_human_reply_preserves_takeover(self):
        human = self._outbound(status="sent", payload={"shvya_hosted": {"origin": "agent"}})
        self.assertEqual(_latest_whatsapp_message(lead=self.lead), human)

    def test_human_reply_on_other_account_does_not_consume_customer_turn(self):
        other = WhatsAppAccount.objects.create(
            organization=self.organization, phone_number_id="other-api", display_phone_number="+918888888888",
            status="connected", is_active=True,
        )
        self._outbound(status="sent", account=other)
        self.assertEqual(_latest_whatsapp_message(lead=self.lead), self.inbound)

    def test_newer_customer_turn_resumes_after_human_reply(self):
        self._outbound(status="sent")
        newer = WhatsAppMessage.objects.create(
            organization=self.organization, account=self.account, lead=self.lead,
            direction="inbound", status="received", body="Another question",
            from_number=self.lead.phone, to_number=self.account.display_phone_number,
        )
        self.assertEqual(_latest_whatsapp_message(lead=self.lead), newer)

    def test_existing_source_response_never_regenerates_or_replays_delivery(self):
        from apps.ai_engagement import tasks

        for status in ("queued", "sending", "sent", "delivered", "read", "failed"):
            with self.subTest(status=status):
                existing = self._outbound(
                    status=status,
                    payload={"shvya_ai": {"source_inbound_message_id": str(self.inbound.id)}},
                )
                with patch("apps.ai_engagement.services.engagement.EngagementService.engage") as engage:
                    result = tasks._execute_ai_engagement_response_impl(
                        task=SimpleNamespace(), lead_id=str(self.lead.id),
                    )
                engage.assert_not_called()
                self.assertEqual(result["message_id"], str(existing.id))
                self.assertEqual(result["delivery_status"], status)
                self.assertEqual(result["status"], "failed" if status == "failed" else "skipped")
                existing.refresh_from_db()
                self.assertEqual(existing.status, status)
                existing.delete()
