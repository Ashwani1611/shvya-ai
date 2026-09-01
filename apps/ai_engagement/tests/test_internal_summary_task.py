from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.ai_engagement.models import InternalConversationSummary
from apps.ai_engagement.services.ai_provider import AITextResult
from apps.ai_engagement.tasks import (
    generate_internal_conversation_summary,
)
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization


class InternalConversationSummaryTaskTests(TestCase):
    """
    Tests the Celery task responsible for generating and publishing
    the internal conversation summary.

    These tests execute the real Celery task body with `.run()`.

    The AI provider itself is mocked.

    These tests do NOT call:

        - Meta
        - OpenAI
        - a real Celery worker
    """

    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(
            name="Internal Summary Task Test Organization",
        )

        cls.pipeline = Pipeline.objects.create(
            organization=cls.organization,
            name="Internal Summary Test Pipeline",
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
            business_name="Internal Summary Test WhatsApp",
            status=(
                WhatsAppAccount.Status.CONNECTED
            ),
            is_active=True,
        )

    # ========================================================
    # HELPERS
    # ========================================================

    def create_lead(
        self,
        *,
        phone: str,
        name: str = "Summary Test Lead",
    ):
        return Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name=name,
            phone=phone,
            lead_source="whatsapp_api",
        )

    def create_message(
        self,
        *,
        lead,
        external_id: str,
        body: str,
        direction=WhatsAppMessage.Direction.INBOUND,
        created_at=None,
    ):
        status = (
            WhatsAppMessage.Status.RECEIVED
            if direction
            == WhatsAppMessage.Direction.INBOUND
            else WhatsAppMessage.Status.SENT
        )

        message = WhatsAppMessage.objects.create(
            organization=self.organization,
            account=self.account,
            lead=lead,
            direction=direction,
            external_id=external_id,
            from_number=lead.phone,
            to_number="TEMP",
            body=body,
            status=status,
            raw_payload={
                "test": True,
            },
            is_read=True,
        )

        if created_at is not None:

            WhatsAppMessage.objects.filter(
                pk=message.pk,
            ).update(
                created_at=created_at,
            )

            message.refresh_from_db()

        return message

    def configure_provider(
        self,
        mocked_provider,
        *,
        text,
        model="gpt-4.1-nano",
    ):
        """
        Configure the mocked OpenAIProvider instance used by
        InternalSummaryService.
        """

        mocked_provider.return_value.generate_text.return_value = (
            AITextResult(
                text=text,
                model=model,
            )
        )

    # ========================================================
    # GENERATION
    # ========================================================

    @patch(
        "apps.ai_engagement.services.internal_summary."
        "OpenAIProvider"
    )
    def test_stale_summary_is_generated_and_published(
        self,
        mocked_provider,
    ):
        """
        A stale or missing summary should be generated through
        the provider and persisted as the active summary.
        """

        lead = self.create_lead(
            phone="+919876543230",
            name="Generation Test Lead",
        )

        message = self.create_message(
            lead=lead,
            external_id="wamid-task-test-001",
            body=(
                "I am interested in Security+ "
                "and want weekend batch details."
            ),
        )

        summary_text = (
            "Lead is interested in Security+ and is "
            "specifically asking about weekend batch details."
        )

        self.configure_provider(
            mocked_provider,
            text=summary_text,
        )

        result = (
            generate_internal_conversation_summary.run(
                str(lead.id)
            )
        )

        self.assertEqual(
            result["status"],
            "completed",
        )

        mocked_provider.return_value.generate_text.assert_called_once()

        summary = (
            InternalConversationSummary.objects
            .filter(
                organization=self.organization,
                lead=lead,
                is_active=True,
            )
            .first()
        )

        self.assertIsNotNone(
            summary
        )

        self.assertEqual(
            summary.summary,
            summary_text,
        )

        self.assertEqual(
            summary.source_message_count,
            1,
        )

        self.assertEqual(
            summary.source_last_message_id,
            message.id,
        )

        self.assertEqual(
            summary.source_last_message_at,
            message.created_at,
        )

        self.assertEqual(
            summary.model_name,
            "gpt-4.1-nano",
        )

        self.assertEqual(
            summary.generated_by,
            "shvya_ai",
        )

        self.assertEqual(
            result["summary_id"],
            str(summary.id),
        )

        mocked_provider.return_value.generate_text.assert_called_once()

        provider_call = (
            mocked_provider.return_value.generate_text.call_args
        )

        self.assertEqual(
            provider_call.kwargs["metadata"]["purpose"],
            "internal_conversation_summary",
        )

        self.assertEqual(
            provider_call.kwargs["metadata"]["organization_id"],
            str(self.organization.id),
        )

        self.assertEqual(
            provider_call.kwargs["metadata"]["lead_id"],
            str(lead.id),
        )

    # ========================================================
    # WATERMARK
    # ========================================================

    @patch(
        "apps.ai_engagement.services.internal_summary."
        "OpenAIProvider"
    )
    def test_summary_uses_latest_message_as_watermark(
        self,
        mocked_provider,
    ):
        """
        The summary must record the ID and timestamp of the
        latest conversation message considered by the service.
        """

        lead = self.create_lead(
            phone="+919876543231",
            name="Watermark Test Lead",
        )

        first_message = self.create_message(
            lead=lead,
            external_id="wamid-task-test-002",
            body="I am comparing Security+ and CEH.",
            created_at=(
                timezone.now()
                - timedelta(minutes=5)
            ),
        )

        latest_message = self.create_message(
            lead=lead,
            external_id="wamid-task-test-003",
            body="Please tell me the weekend batch timing.",
        )

        self.assertNotEqual(
            first_message.id,
            latest_message.id,
        )

        self.configure_provider(
            mocked_provider,
            text=(
                "Lead is comparing Security+ and CEH "
                "and wants weekend batch timing."
            ),
        )

        result = (
            generate_internal_conversation_summary.run(
                str(lead.id)
            )
        )

        self.assertEqual(
            result["status"],
            "completed",
        )

        summary = (
            InternalConversationSummary.objects
            .filter(
                organization=self.organization,
                lead=lead,
                is_active=True,
            )
            .get()
        )

        self.assertEqual(
            summary.source_message_count,
            2,
        )

        self.assertEqual(
            summary.source_last_message_id,
            latest_message.id,
        )

        self.assertEqual(
            summary.source_last_message_at,
            latest_message.created_at,
        )

    # ========================================================
    # FRESH SUMMARY GATE
    # ========================================================

    @patch(
        "apps.ai_engagement.services.internal_summary."
        "OpenAIProvider"
    )
    def test_fresh_summary_skips_generation(
        self,
        mocked_provider,
    ):
        """
        If the current summary already covers the latest
        conversation message, the task must not call the AI
        provider again.
        """

        lead = self.create_lead(
            phone="+919876543232",
            name="Fresh Summary Test Lead",
        )

        message = self.create_message(
            lead=lead,
            external_id="wamid-task-test-004",
            body="I want the Security+ course details.",
        )

        existing_summary = (
            InternalConversationSummary.objects.create(
                organization=self.organization,
                lead=lead,
                summary=(
                    "Lead wants Security+ course details."
                ),
                source_message_count=1,
                source_last_message_id=message.id,
                source_last_message_at=message.created_at,
                generated_by="shvya_ai",
                model_name="gpt-4.1-nano",
                is_active=True,
            )
        )

        result = (
            generate_internal_conversation_summary.run(
                str(lead.id)
            )
        )

        self.assertEqual(
            result["status"],
            "skipped",
        )

        self.assertEqual(
            result["reason"],
            "summary_current",
        )

        self.assertEqual(
            result["summary_id"],
            str(existing_summary.id),
        )

        mocked_provider.assert_not_called()

        self.assertEqual(
            InternalConversationSummary.objects.filter(
                organization=self.organization,
                lead=lead,
                is_active=True,
            ).count(),
            1,
        )

    # ========================================================
    # NEW MESSAGE MAKES SUMMARY STALE
    # ========================================================

    @patch(
        "apps.ai_engagement.services.internal_summary."
        "OpenAIProvider"
    )
    def test_new_message_causes_regeneration(
        self,
        mocked_provider,
    ):
        """
        A new WhatsApp message after the current summary's
        watermark must cause a new summary generation.
        """

        lead = self.create_lead(
            phone="+919876543233",
            name="Stale Summary Test Lead",
        )

        first_message = self.create_message(
            lead=lead,
            external_id="wamid-task-test-005",
            body="I am interested in CEH.",
            created_at=(
                timezone.now()
                - timedelta(minutes=5)
            ),
        )

        existing_summary = (
            InternalConversationSummary.objects.create(
                organization=self.organization,
                lead=lead,
                summary="Lead is interested in CEH.",
                source_message_count=1,
                source_last_message_id=first_message.id,
                source_last_message_at=first_message.created_at,
                generated_by="shvya_ai",
                model_name="gpt-4.1-nano",
                is_active=True,
            )
        )

        latest_message = self.create_message(
            lead=lead,
            external_id="wamid-task-test-006",
            body="Also tell me the weekend batch timing.",
        )

        self.configure_provider(
            mocked_provider,
            text=(
                "Lead is interested in CEH and wants "
                "weekend batch timing."
            ),
        )

        result = (
            generate_internal_conversation_summary.run(
                str(lead.id)
            )
        )

        self.assertEqual(
            result["status"],
            "completed",
        )

        mocked_provider.return_value.generate_text.assert_called_once()

        existing_summary.refresh_from_db()

        self.assertFalse(
            existing_summary.is_active
        )

        active_summaries = (
            InternalConversationSummary.objects
            .filter(
                organization=self.organization,
                lead=lead,
                is_active=True,
            )
        )

        self.assertEqual(
            active_summaries.count(),
            1,
        )

        new_summary = active_summaries.get()

        self.assertNotEqual(
            new_summary.id,
            existing_summary.id,
        )

        self.assertEqual(
            new_summary.source_last_message_id,
            latest_message.id,
        )

        self.assertEqual(
            new_summary.source_message_count,
            2,
        )

        self.assertEqual(
            new_summary.model_name,
            "gpt-4.1-nano",
        )

    # ========================================================
    # NO MESSAGE
    # ========================================================

    @patch(
        "apps.ai_engagement.services.internal_summary."
        "OpenAIProvider"
    )
    def test_no_messages_skips_generation(
        self,
        mocked_provider,
    ):
        """
        A Lead without WhatsApp messages must not attempt
        summary generation.
        """

        lead = self.create_lead(
            phone="+919876543234",
            name="No Message Test Lead",
        )

        result = (
            generate_internal_conversation_summary.run(
                str(lead.id)
            )
        )

        self.assertEqual(
            result["status"],
            "skipped",
        )

        self.assertEqual(
            result["reason"],
            "no_messages",
        )

        mocked_provider.assert_not_called()

        self.assertEqual(
            InternalConversationSummary.objects.filter(
                organization=self.organization,
                lead=lead,
            ).count(),
            0,
        )

    # ========================================================
    # LEAD NOT FOUND
    # ========================================================

    @patch(
        "apps.ai_engagement.services.internal_summary."
        "OpenAIProvider"
    )
    def test_missing_lead_skips_generation(
        self,
        mocked_provider,
    ):
        """
        A missing Lead must result in a clean skip rather than
        an exception.
        """

        result = (
            generate_internal_conversation_summary.run(
                "00000000-0000-0000-0000-000000000000"
            )
        )

        self.assertEqual(
            result["status"],
            "skipped",
        )

        self.assertEqual(
            result["reason"],
            "lead_not_found",
        )

        mocked_provider.assert_not_called()