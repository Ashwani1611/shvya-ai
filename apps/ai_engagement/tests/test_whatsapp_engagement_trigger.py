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
        "apps.ai_engagement.tasks.generate_ai_engagement_response.apply_async"
    )
    @patch(
        "apps.ai_engagement.background_signals.queue_background_enrichment"
    )
    def test_inbound_message_queues_engagement_after_commit(
        self,
        background_enrichment,
        engagement_apply_async,
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

        background_enrichment.assert_called_once_with(
            lead_id=str(self.lead.id)
        )
        engagement_apply_async.assert_called_once_with(
            args=[str(self.lead.id)],
            countdown=0,
        )

    @patch(
        "apps.ai_engagement.tasks.generate_ai_engagement_response.apply_async"
    )
    @patch(
        "apps.ai_engagement.background_signals.queue_background_enrichment"
    )
    def test_duplicate_inbound_external_id_does_not_queue_again(
        self,
        background_enrichment,
        engagement_apply_async,
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
        background_enrichment.assert_called_once_with(
            lead_id=str(self.lead.id)
        )
        engagement_apply_async.assert_called_once_with(
            args=[str(self.lead.id)],
            countdown=0,
        )

    @patch(
        "apps.ai_engagement.tasks.generate_ai_engagement_response.apply_async"
    )
    def test_outbound_message_does_not_queue_engagement(
        self,
        engagement_apply_async,
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

        engagement_apply_async.assert_not_called()

    @patch(
        "apps.ai_engagement.tasks.generate_ai_engagement_response.apply_async"
    )
    def test_unattached_inbound_message_does_not_queue_engagement(
        self,
        engagement_apply_async,
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

        engagement_apply_async.assert_not_called()

    @patch("apps.ai_engagement.tasks.generate_ai_engagement_response.apply_async")
    @patch("apps.ai_engagement.background_signals.queue_background_enrichment")
    def test_negative_inbound_messages_keep_ai_enabled_and_queue_response(self, enrichment, enqueue):
        for index, body in enumerate(["STOP", "no", "not interested", "hello"]):
            with self.captureOnCommitCallbacks(execute=True):
                handle_inbound_message(
                    organization=self.organization, account=self.account,
                    external_id=f"wamid-policy-{index}", from_number=self.lead.phone,
                    to_number="919999999999", body=body, raw_payload={"test": True},
                )
            self.lead.refresh_from_db()
            self.assertTrue(self.lead.ai_enabled)
        self.assertEqual(enqueue.call_count, 4)
