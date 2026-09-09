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

    @patch("apps.ai_engagement.services.qualification.QualificationService.generate")
    def test_task_creates_and_deduplicates_validated_summary(self, generate):
        from apps.ai_engagement.services.qualification import QualificationResult
        from apps.crm.models import LeadNote
        lead = self.create_lead(phone="+919876543251")
        self.create_message(lead=lead, external_id="task-answer", body="Security+")
        generate.return_value = QualificationResult(summary="Course: Security+", model="test")
        first = generate_lead_qualification.run(str(lead.id))
        self.assertEqual(first["status"], "completed")
        second = generate_lead_qualification.run(str(lead.id))
        self.assertEqual(second["reason"], "qualification_unchanged")
        generate.return_value = QualificationResult(summary="Timing: Weekends", model="test")
        third = generate_lead_qualification.run(str(lead.id))
        self.assertEqual(third["note_id"], first["note_id"])
        note = LeadNote.objects.get(pk=third["note_id"])
        self.assertIn("Timing: Weekends", note.note)
        self.assertLessEqual(len(note.note), 500)

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
