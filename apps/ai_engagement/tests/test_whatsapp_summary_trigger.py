from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Pipeline, Stage
from apps.organizations.models import Organization
from services.channels.whatsapp_service import handle_inbound_message
from apps.ai_engagement.services.turn_burst import turn_burst_seconds


class WhatsAppSummaryTriggerTests(TestCase):
    """
    Tests the WhatsApp -> reply -> post-turn summary contract.

    Inbound messages queue the realtime reply after the shared burst window.
    The expensive summary call is scheduled only after an AI outbound row is
    persisted, so a normal successful turn is reply generation then summary.
    """

    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(
            name="Summary Trigger Test Organization",
        )

        cls.pipeline = Pipeline.objects.create(
            organization=cls.organization,
            name="Summary Trigger Test Pipeline",
            is_active=True,
        )

        cls.stage = Stage.objects.create(
            pipeline=cls.pipeline,
            name="New",
            display_order=0,
            is_active=True,
        )

        cls.account = WhatsAppAccount.objects.create(
            organization=cls.organization,
            connection_type=(
                WhatsAppAccount.ConnectionType.API
            ),
            business_name="Summary Trigger Test WhatsApp",
            status=(
                WhatsAppAccount.Status.CONNECTED
            ),
            is_active=True,
        )

    def setUp(self):
        self.ai_engagement_patcher = patch(
            "apps.ai_engagement.tasks."
            "generate_ai_engagement_response.apply_async"
        )
        self.mocked_ai_engagement_apply_async = (
            self.ai_engagement_patcher.start()
        )
        self.addCleanup(
            self.ai_engagement_patcher.stop
        )

    @patch(
        "apps.ai_engagement.background_signals."
        "queue_background_enrichment"
    )
    def test_inbound_message_defers_summary_until_ai_reply(
        self,
        mocked_enrichment,
    ):
        """A new inbound Lead message queues only the realtime reply turn."""

        phone = "+919876543223"

        with self.captureOnCommitCallbacks(
            execute=True,
        ):
            message = handle_inbound_message(
                organization=self.organization,
                account=self.account,
                external_id="wamid-summary-trigger-001",
                from_number=phone,
                to_number="TEMP",
                body="I am interested in the cybersecurity course.",
                raw_payload={"test": True},
            )

        self.assertIsNotNone(message.pk)
        self.assertIsNotNone(message.lead_id)

        mocked_enrichment.assert_not_called()
        self.mocked_ai_engagement_apply_async.assert_called_once_with(
            args=[str(message.lead_id)],
            countdown=turn_burst_seconds(),
        )

    @patch(
        "apps.ai_engagement.background_signals."
        "queue_background_enrichment"
    )
    def test_duplicate_external_id_does_not_queue_again(
        self,
        mocked_enrichment,
    ):
        """A duplicate Meta external_id must not schedule enrichment twice."""

        phone = "+919876543224"

        with self.captureOnCommitCallbacks(execute=True):
            first_message = handle_inbound_message(
                organization=self.organization,
                account=self.account,
                external_id="wamid-summary-trigger-002",
                from_number=phone,
                to_number="TEMP",
                body="First inbound message.",
                raw_payload={"test": True},
            )

            second_message = handle_inbound_message(
                organization=self.organization,
                account=self.account,
                external_id="wamid-summary-trigger-002",
                from_number=phone,
                to_number="TEMP",
                body="Duplicate webhook delivery.",
                raw_payload={"test": True, "duplicate": True},
            )

        self.assertEqual(first_message.pk, second_message.pk)
        self.assertEqual(
            WhatsAppMessage.objects.filter(
                organization=self.organization,
                external_id="wamid-summary-trigger-002",
            ).count(),
            1,
        )
        mocked_enrichment.assert_not_called()
        self.mocked_ai_engagement_apply_async.assert_called_once_with(
            args=[str(first_message.lead_id)],
            countdown=turn_burst_seconds(),
        )

    @patch(
        "apps.ai_engagement.background_signals."
        "queue_background_enrichment"
    )
    def test_inbound_does_not_spend_summary_call(
        self,
        mocked_enrichment,
    ):
        """Inbound processing does not queue the post-turn summary before a reply exists."""

        phone = "+919876543225"

        with self.captureOnCommitCallbacks(execute=True):
            message = handle_inbound_message(
                organization=self.organization,
                account=self.account,
                external_id="wamid-summary-trigger-003",
                from_number=phone,
                to_number="TEMP",
                body="Please send me the weekend batch details.",
                raw_payload={"test": True},
            )

        mocked_enrichment.assert_not_called()
        self.mocked_ai_engagement_apply_async.assert_called_once_with(
            args=[str(message.lead_id)],
            countdown=turn_burst_seconds(),
        )

    @patch(
        "apps.ai_engagement.background_signals.cache.add",
        return_value=True,
    )
    @patch(
        "apps.ai_engagement.background_signals.queue_background_enrichment"
    )
    def test_ai_outbound_schedules_one_post_turn_summary(
        self,
        mocked_enrichment,
        mocked_cache_add,
    ):
        phone = "+919876543226"

        with self.captureOnCommitCallbacks(execute=True):
            inbound = handle_inbound_message(
                organization=self.organization,
                account=self.account,
                external_id="wamid-summary-trigger-004",
                from_number=phone,
                to_number="TEMP",
                body="Tell me the plan details.",
                raw_payload={"test": True},
            )

        mocked_enrichment.reset_mock()
        with self.captureOnCommitCallbacks(execute=True):
            WhatsAppMessage.objects.create(
                organization=self.organization,
                account=self.account,
                lead=inbound.lead,
                direction=WhatsAppMessage.Direction.OUTBOUND,
                from_number=self.account.display_phone_number,
                to_number=phone,
                body="Here are the plan details.",
                status=WhatsAppMessage.Status.QUEUED,
                raw_payload={
                    "shvya_ai": {
                        "source_inbound_message_id": str(inbound.pk),
                        "model": "test",
                    }
                },
            )

        mocked_cache_add.assert_called_once()
        mocked_enrichment.assert_called_once_with(
            lead_id=str(inbound.lead_id),
            force=True,
            include_qualification=False,
        )

