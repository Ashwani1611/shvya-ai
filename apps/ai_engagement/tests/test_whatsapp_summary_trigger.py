from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization
from services.channels.whatsapp_service import handle_inbound_message


class WhatsAppSummaryTriggerTests(TestCase):
    """
    Tests the WhatsApp -> internal conversation-summary Celery trigger.

    These tests do not call:

        - Meta
        - OpenAI
        - a real Celery worker

    Celery's delay() method is mocked.

    The on_commit callbacks are explicitly executed by using
    Django's captureOnCommitCallbacks(execute=True), because
    Django TestCase normally keeps transaction callbacks from
    executing until the surrounding test transaction completes.
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
        # The inbound-message signal queues both summary generation
        # and customer-facing AI engagement. These tests cover only
        # the summary trigger, so prevent eager Celery execution of
        # the separate engagement task (and therefore OpenAI calls).
        self.ai_engagement_patcher = patch(
            "apps.ai_engagement.tasks."
            "generate_ai_engagement_response.delay"
        )
        self.mocked_ai_engagement_delay = (
            self.ai_engagement_patcher.start()
        )
        self.addCleanup(
            self.ai_engagement_patcher.stop
        )

    @patch(
        "apps.ai_engagement.tasks."
        "generate_internal_conversation_summary.delay"
    )
    def test_inbound_message_queues_summary(
        self,
        mocked_delay,
    ):
        """
        A new inbound WhatsApp message attached to a Lead
        queues exactly one summary task after commit.
        """

        phone = "+919876543223"

        with self.captureOnCommitCallbacks(
            execute=True,
        ):

            message = handle_inbound_message(
                organization=self.organization,
                account=self.account,
                external_id=(
                    "wamid-summary-trigger-001"
                ),
                from_number=phone,
                to_number="TEMP",
                body=(
                    "I am interested in the "
                    "cybersecurity course."
                ),
                raw_payload={
                    "test": True,
                },
            )

        self.assertIsNotNone(
            message.pk
        )

        self.assertIsNotNone(
            message.lead_id
        )

        mocked_delay.assert_called_once_with(
            str(message.lead_id)
        )

        self.mocked_ai_engagement_delay.assert_called_once_with(
            str(message.lead_id)
        )

    @patch(
        "apps.ai_engagement.tasks."
        "generate_internal_conversation_summary.delay"
    )
    def test_duplicate_external_id_does_not_queue_again(
        self,
        mocked_delay,
    ):
        """
        Repeated delivery of the same Meta external_id must:

            - return the existing message
            - not create a second message
            - not queue a second summary task
        """

        phone = "+919876543224"

        with self.captureOnCommitCallbacks(
            execute=True,
        ):

            first_message = handle_inbound_message(
                organization=self.organization,
                account=self.account,
                external_id=(
                    "wamid-summary-trigger-002"
                ),
                from_number=phone,
                to_number="TEMP",
                body="First inbound message.",
                raw_payload={
                    "test": True,
                },
            )

            second_message = handle_inbound_message(
                organization=self.organization,
                account=self.account,
                external_id=(
                    "wamid-summary-trigger-002"
                ),
                from_number=phone,
                to_number="TEMP",
                body="Duplicate webhook delivery.",
                raw_payload={
                    "test": True,
                    "duplicate": True,
                },
            )

        self.assertEqual(
            first_message.pk,
            second_message.pk,
        )

        self.assertEqual(
            WhatsAppMessage.objects.filter(
                organization=self.organization,
                external_id=(
                    "wamid-summary-trigger-002"
                ),
            ).count(),
            1,
        )

        mocked_delay.assert_called_once_with(
            str(first_message.lead_id)
        )

        self.mocked_ai_engagement_delay.assert_called_once_with(
            str(first_message.lead_id)
        )

    @patch(
        "apps.ai_engagement.tasks."
        "generate_internal_conversation_summary.delay"
    )
    def test_summary_task_receives_only_lead_id(
        self,
        mocked_delay,
    ):
        """
        The WhatsApp service sends only the Lead ID to Celery.

        The worker resolves the current Lead and conversation
        state itself.
        """

        phone = "+919876543225"

        with self.captureOnCommitCallbacks(
            execute=True,
        ):

            message = handle_inbound_message(
                organization=self.organization,
                account=self.account,
                external_id=(
                    "wamid-summary-trigger-003"
                ),
                from_number=phone,
                to_number="TEMP",
                body=(
                    "Please send me the weekend "
                    "batch details."
                ),
                raw_payload={
                    "test": True,
                },
            )

        mocked_delay.assert_called_once_with(
            str(message.lead_id)
        )

        self.mocked_ai_engagement_delay.assert_called_once_with(
            str(message.lead_id)
        )
