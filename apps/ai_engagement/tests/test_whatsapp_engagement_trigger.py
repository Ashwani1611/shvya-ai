from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization
from services.channels.whatsapp_service import handle_inbound_message


class WhatsAppEngagementTriggerTests(TestCase):
    """WhatsApp inbound messages must queue the canonical AI worker."""

    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(
            name="WhatsApp Engagement Test Organization",
        )

        cls.pipeline = Pipeline.objects.create(
            organization=cls.organization,
            name="WhatsApp Engagement Pipeline",
            description="Pipeline used for WhatsApp engagement tests.",
            is_active=True,
        )

        cls.stage = Stage.objects.create(
            pipeline=cls.pipeline,
            name="New Lead",
            description="New incoming lead.",
            display_order=0,
            is_active=True,
            ai_on=True,
        )

        cls.account = WhatsAppAccount.objects.create(
            organization=cls.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="WhatsApp Engagement Test",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )

        cls.lead = Lead.objects.create(
            organization=cls.organization,
            pipeline=cls.pipeline,
            stage=cls.stage,
            name="WhatsApp Engagement Lead",
            phone="+919876543210",
            email="lead@example.com",
            notes="",
            attributes={},
            lead_source="whatsapp_api",
            ai_enabled=True,
        )

    @patch(
        "apps.ai_engagement.tasks.generate_ai_engagement_response.delay"
    )
    @patch(
        "apps.ai_engagement.tasks."
        "generate_internal_conversation_summary.delay"
    )
    def test_inbound_message_queues_engagement_after_commit(
        self,
        summary_delay,
        engagement_delay,
    ):
        with self.captureOnCommitCallbacks(execute=True):
            handle_inbound_message(
                organization=self.organization,
                account=self.account,
                external_id="wamid-trigger-001",
                from_number=self.lead.phone,
                to_number="919999999999",
                body="Tell me more about the course.",
                raw_payload={"test": True},
            )

        summary_delay.assert_called_once_with(str(self.lead.id))
        engagement_delay.assert_called_once_with(str(self.lead.id))

    @patch(
        "apps.ai_engagement.tasks.generate_ai_engagement_response.delay"
    )
    @patch(
        "apps.ai_engagement.tasks."
        "generate_internal_conversation_summary.delay"
    )
    def test_duplicate_inbound_external_id_does_not_queue_again(
        self,
        summary_delay,
        engagement_delay,
    ):
        kwargs = {
            "organization": self.organization,
            "account": self.account,
            "external_id": "wamid-trigger-duplicate",
            "from_number": self.lead.phone,
            "to_number": "919999999999",
            "body": "I am interested.",
            "raw_payload": {"test": True},
        }

        with self.captureOnCommitCallbacks(execute=True):
            handle_inbound_message(**kwargs)

        with self.captureOnCommitCallbacks(execute=True):
            handle_inbound_message(**kwargs)

        self.assertEqual(
            WhatsAppMessage.objects.filter(
                external_id="wamid-trigger-duplicate",
            ).count(),
            1,
        )
        summary_delay.assert_called_once_with(str(self.lead.id))
        engagement_delay.assert_called_once_with(str(self.lead.id))

    @patch(
        "apps.ai_engagement.tasks.generate_ai_engagement_response.delay"
    )
    def test_outbound_message_does_not_queue_engagement(
        self,
        engagement_delay,
    ):
        with self.captureOnCommitCallbacks(execute=True):
            WhatsAppMessage.objects.create(
                organization=self.organization,
                account=self.account,
                lead=self.lead,
                direction=WhatsAppMessage.Direction.OUTBOUND,
                from_number="919999999999",
                to_number=self.lead.phone,
                body="Outbound message",
                status=WhatsAppMessage.Status.QUEUED,
            )

        engagement_delay.assert_not_called()

    @patch(
        "apps.ai_engagement.tasks.generate_ai_engagement_response.delay"
    )
    def test_unattached_inbound_message_does_not_queue_engagement(
        self,
        engagement_delay,
    ):
        with self.captureOnCommitCallbacks(execute=True):
            WhatsAppMessage.objects.create(
                organization=self.organization,
                account=self.account,
                lead=None,
                direction=WhatsAppMessage.Direction.INBOUND,
                external_id="wamid-unattached",
                from_number="+919000000000",
                to_number="919999999999",
                body="Hello",
                status=WhatsAppMessage.Status.RECEIVED,
                raw_payload={"test": True},
                is_read=False,
            )

        engagement_delay.assert_not_called()
