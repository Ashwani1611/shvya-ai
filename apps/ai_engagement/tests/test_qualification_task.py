from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from apps.ai_engagement.models import (
    InternalConversationSummary,
    OrgInfo,
)
from apps.ai_engagement.services.ai_provider import (
    AIProviderPermanentError,
    AIProviderTransientError,
    AITextResult,
)
from apps.ai_engagement.tasks import (
    generate_lead_qualification,
)
from apps.channels.models import (
    WhatsAppAccount,
    WhatsAppMessage,
)
from apps.crm.models import (
    Lead,
    Pipeline,
    Stage,
)
from apps.organizations.models import Organization


class LeadQualificationTaskTests(TestCase):
    """
    Tests the Celery task responsible for generating and
    appending AI qualification summaries.

    The task body is executed directly with `.run()`.

    OpenAI itself is always mocked.
    """

    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(
            name="Qualification Task Test Organization",
        )

        OrgInfo.objects.create(
            organization=cls.organization,
            about=(
                "Cybersecurity training academy "
                "providing certification programs."
            ),
            bot_languages=(
                "English, Hindi, Hinglish"
            ),
            qualification_requirements=(
                "Identify the learner's preferred course, "
                "preferred batch timing, main training goal, "
                "current lead volume, and buying intent."
            ),
            ai_enabled=True,
        )

        cls.pipeline = Pipeline.objects.create(
            organization=cls.organization,
            name="Qualification Task Pipeline",
            is_active=True,
        )

        cls.stage = Stage.objects.create(
            pipeline=cls.pipeline,
            name="New",
            description="New qualification lead.",
            display_order=0,
            is_active=True,
        )

        cls.account = WhatsAppAccount.objects.create(
            organization=cls.organization,
            connection_type=(
                WhatsAppAccount.ConnectionType.API
            ),
            business_name="Qualification Task WhatsApp",
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
        name: str = "Qualification Task Lead",
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
    ):
        return WhatsAppMessage.objects.create(
            organization=self.organization,
            account=self.account,
            lead=lead,
            direction=WhatsAppMessage.Direction.INBOUND,
            external_id=external_id,
            from_number=lead.phone,
            to_number="TEMP",
            body=body,
            status=WhatsAppMessage.Status.RECEIVED,
            raw_payload={
                "test": True,
            },
            is_read=True,
        )

    def mock_successful_provider(
        self,
        mocked_provider,
        *,
        summary: str,
    ):
        mocked_provider.return_value.generate_text.return_value = (
            AITextResult(
                text=summary,
                model="gpt-4.1-nano",
            )
        )

    # ========================================================
    # LEAD NOT FOUND
    # ========================================================

    @patch(
        "apps.ai_engagement.services.qualification."
        "OpenAIProvider"
    )
    def test_missing_lead_is_skipped(
        self,
        mocked_provider,
    ):
        """
        Missing Lead must be a clean skip.
        """

        result = (
            generate_lead_qualification.run(
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

    # ========================================================
    # NO MESSAGES
    # ========================================================

    @patch(
        "apps.ai_engagement.services.qualification."
        "OpenAIProvider"
    )
    def test_lead_without_messages_is_skipped(
        self,
        mocked_provider,
    ):
        """
        A Lead without WhatsApp messages should not invoke AI.
        """

        lead = self.create_lead(
            phone="+919876543250",
        )

        result = (
            generate_lead_qualification.run(
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

    # ========================================================
    # FIRST QUALIFICATION
    # ========================================================

    @patch(
        "apps.ai_engagement.services.qualification."
        "OpenAIProvider"
    )
    def test_task_creates_initial_qualification_note(
        self,
        mocked_provider,
    ):
        """
        A Lead with a conversation should receive an initial
        AI qualification system note.
        """

        lead = self.create_lead(
            phone="+919876543251",
        )

        self.create_message(
            lead=lead,
            external_id="qualification-task-001",
            body=(
                "I want Security+ and prefer "
                "weekend classes."
            ),
        )

        self.mock_successful_provider(
            mocked_provider,
            summary=(
                "Lead is interested in Security+ and "
                "prefers weekend classes."
            ),
        )

        result = (
            generate_lead_qualification.run(
                str(lead.id)
            )
        )

        self.assertEqual(
            result["status"],
            "completed",
        )

        self.assertEqual(
            result["note_type"],
            "system",
        )

        self.assertTrue(
            result["note_id"]
        )

        mocked_provider.return_value.generate_text.assert_called_once()

        from apps.crm.models import LeadNote

        note = LeadNote.objects.get(
            id=result["note_id"]
        )

        self.assertEqual(
            note.lead_id,
            lead.id,
        )

        self.assertEqual(
            note.note_type,
            "system",
        )

        self.assertIn(
            "<AI Qualification Summary - ",
            note.note,
        )

        self.assertIn(
            "Lead is interested in Security+",
            note.note,
        )

    # ========================================================
    # UNCHANGED QUALIFICATION
    # ========================================================

    @patch(
        "apps.ai_engagement.services.qualification."
        "OpenAIProvider"
    )
    def test_unchanged_qualification_is_skipped(
        self,
        mocked_provider,
    ):
        """
        When AI produces the same qualification result,
        the task should not append another update.
        """

        lead = self.create_lead(
            phone="+919876543252",
        )

        self.create_message(
            lead=lead,
            external_id="qualification-task-002",
            body=(
                "I want Security+ on weekends."
            ),
        )

        qualification_text = (
            "Lead is interested in Security+ and "
            "prefers weekend classes."
        )

        self.mock_successful_provider(
            mocked_provider,
            summary=qualification_text,
        )

        first_result = (
            generate_lead_qualification.run(
                str(lead.id)
            )
        )

        self.assertEqual(
            first_result["status"],
            "completed",
        )

        second_result = (
            generate_lead_qualification.run(
                str(lead.id)
            )
        )

        self.assertEqual(
            second_result["status"],
            "skipped",
        )

        self.assertEqual(
            second_result["reason"],
            "qualification_unchanged",
        )

        from apps.crm.models import LeadNote

        note = LeadNote.objects.get(
            id=first_result["note_id"]
        )

        self.assertEqual(
            note.note.count(
                "***** Updated Summary"
            ),
            0,
        )

        self.assertEqual(
            note.note.count(
                qualification_text
            ),
            1,
        )

    # ========================================================
    # CHANGED QUALIFICATION
    # ========================================================

    @patch(
        "apps.ai_engagement.services.qualification."
        "OpenAIProvider"
    )
    def test_changed_qualification_is_appended(
        self,
        mocked_provider,
    ):
        """
        A changed qualification result should append an
        Updated Summary entry.
        """

        lead = self.create_lead(
            phone="+919876543253",
        )

        self.create_message(
            lead=lead,
            external_id="qualification-task-003",
            body=(
                "I am considering Security+."
            ),
        )

        self.mock_successful_provider(
            mocked_provider,
            summary=(
                "Lead is considering Security+."
            ),
        )

        first_result = (
            generate_lead_qualification.run(
                str(lead.id)
            )
        )

        self.assertEqual(
            first_result["status"],
            "completed",
        )

        self.mock_successful_provider(
            mocked_provider,
            summary=(
                "Lead confirmed Security+ as the preferred "
                "course and wants a weekend batch."
            ),
        )

        second_result = (
            generate_lead_qualification.run(
                str(lead.id)
            )
        )

        self.assertEqual(
            second_result["status"],
            "completed",
        )

        self.assertEqual(
            second_result["note_id"],
            first_result["note_id"],
        )

        from apps.crm.models import LeadNote

        note = LeadNote.objects.get(
            id=first_result["note_id"]
        )

        self.assertEqual(
            note.note.count(
                "***** Updated Summary"
            ),
            1,
        )

        self.assertIn(
            "Lead is considering Security+.",
            note.note,
        )

        self.assertIn(
            "Lead confirmed Security+",
            note.note,
        )

    # ========================================================
    # CURRENT CONVERSATION SUMMARY IS AVAILABLE
    # ========================================================

    @patch(
        "apps.ai_engagement.services.qualification."
        "OpenAIProvider"
    )
    def test_task_can_use_current_conversation_summary(
        self,
        mocked_provider,
    ):
        """
        Qualification task should be able to consume the
        existing Conversation Summary through AIContextBuilder.
        """

        lead = self.create_lead(
            phone="+919876543254",
        )

        message = self.create_message(
            lead=lead,
            external_id="qualification-task-004",
            body=(
                "I receive around 20 leads a day "
                "and need WhatsApp automation."
            ),
        )

        InternalConversationSummary.objects.create(
            organization=self.organization,
            lead=lead,
            summary=(
                "Lead receives around 20 leads daily "
                "and wants WhatsApp automation."
            ),
            source_message_count=1,
            source_last_message_id=message.id,
            source_last_message_at=message.created_at,
            generated_by="shvya_ai",
            model_name="gpt-4.1-nano",
            is_active=True,
        )

        self.mock_successful_provider(
            mocked_provider,
            summary=(
                "Lead appears interested in WhatsApp "
                "automation and currently receives "
                "around 20 leads daily."
            ),
        )

        result = (
            generate_lead_qualification.run(
                str(lead.id)
            )
        )

        self.assertEqual(
            result["status"],
            "completed",
        )

        provider_call = (
            mocked_provider.return_value.generate_text.call_args
        )

        provider_input = (
            provider_call.kwargs["input_text"]
        )

        self.assertIn(
            "CURRENT CONVERSATION SUMMARY",
            provider_input,
        )

        self.assertIn(
            "Lead receives around 20 leads daily",
            provider_input,
        )

        self.assertIn(
            "ACTUAL CONVERSATION",
            provider_input,
        )

    # ========================================================
    # TRANSIENT PROVIDER FAILURE
    # ========================================================

    @patch(
        "apps.ai_engagement.services.qualification."
        "OpenAIProvider"
    )
    @patch(
        "apps.ai_engagement.tasks."
        "generate_lead_qualification.retry"
    )
    def test_transient_provider_failure_requests_retry(
        self,
        mocked_retry,
        mocked_provider,
    ):
        """
        A transient provider failure should be propagated to
        the Celery task's retry mechanism.
        """

        lead = self.create_lead(
            phone="+919876543255",
        )

        self.create_message(
            lead=lead,
            external_id="qualification-task-005",
            body="I want more information.",
        )

        mocked_provider.return_value.generate_text.side_effect = (
            AIProviderTransientError(
                "temporary provider failure"
            )
        )

        retry_exception = RuntimeError(
            "celery retry requested"
        )

        mocked_retry.side_effect = retry_exception

        with self.assertRaises(
            RuntimeError
        ):

            generate_lead_qualification.run(
                str(lead.id)
            )

        mocked_retry.assert_called_once()

        retry_call = (
            mocked_retry.call_args
        )

        self.assertEqual(
            retry_call.kwargs["countdown"],
            60,
        )

    # ========================================================
    # PERMANENT PROVIDER FAILURE
    # ========================================================

    @patch(
        "apps.ai_engagement.services.qualification."
        "OpenAIProvider"
    )
    @patch(
        "apps.ai_engagement.tasks."
        "generate_lead_qualification.retry"
    )
    def test_permanent_provider_failure_does_not_retry(
        self,
        mocked_retry,
        mocked_provider,
    ):
        """
        Permanent provider failures should become a clean
        failed task result rather than an automatic retry.
        """

        lead = self.create_lead(
            phone="+919876543256",
        )

        self.create_message(
            lead=lead,
            external_id="qualification-task-006",
            body="Please explain the course.",
        )

        mocked_provider.return_value.generate_text.side_effect = (
            AIProviderPermanentError(
                "permanent provider failure"
            )
        )

        result = (
            generate_lead_qualification.run(
                str(lead.id)
            )
        )

        self.assertEqual(
            result["status"],
            "failed",
        )

        self.assertEqual(
            result["reason"],
            "qualification_generation_failed",
        )

        mocked_retry.assert_not_called()