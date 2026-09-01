from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from apps.ai_engagement.models import (
    InternalConversationSummary,
    OrgInfo,
)
from apps.ai_engagement.services.ai_provider import (
    AITextResult,
)
from apps.ai_engagement.services.qualification import (
    QualificationService,
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


class QualificationServiceTests(TestCase):
    """
    Tests the AI Qualification Summary service.

    The OpenAI provider is mocked.

    These tests do NOT call:

        - OpenAI
        - Meta
        - Celery
    """

    @classmethod
    def setUpTestData(cls):

        cls.organization = Organization.objects.create(
            name="Qualification Test Organization",
        )

        OrgInfo.objects.create(
            organization=cls.organization,
            about=(
                "Cybersecurity training academy "
                "providing certification-focused programs."
            ),
            bot_languages=(
                "English, Hindi, Hinglish"
            ),
            qualification_requirements=(
                "Identify the learner's preferred course, "
                "preferred batch timing, main training goal, "
                "current lead volume, and buying intent. "
                "Identify missing qualification information "
                "without inventing facts."
            ),
            ai_enabled=True,
        )

        cls.pipeline = Pipeline.objects.create(
            organization=cls.organization,
            name="Qualification Pipeline",
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
            business_name="Qualification Test WhatsApp",
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
        phone: str,
        name: str = "Qualification Lead",
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
        external_id,
        body,
        direction=WhatsAppMessage.Direction.INBOUND,
    ):
        status = (
            WhatsAppMessage.Status.RECEIVED
            if direction
            == WhatsAppMessage.Direction.INBOUND
            else WhatsAppMessage.Status.SENT
        )

        return WhatsAppMessage.objects.create(
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

    def create_conversation_summary(
        self,
        *,
        lead,
        summary,
        source_message_count=1,
    ):
        return (
            InternalConversationSummary.objects.create(
                organization=self.organization,
                lead=lead,
                summary=summary,
                source_message_count=(
                    source_message_count
                ),
                generated_by="shvya_ai",
                model_name="gpt-4.1-nano",
                is_active=True,
            )
        )

    # ========================================================
    # CONTEXT
    # ========================================================

    def test_provider_input_includes_qualification_requirements(
        self,
    ):
        """
        Organization qualification requirements must be included
        in the provider input.
        """

        lead = self.create_lead(
            "+919876543240"
        )

        self.create_message(
            lead=lead,
            external_id="qualification-input-001",
            body=(
                "I want Security+ and prefer "
                "weekend batches."
            ),
        )

        service = QualificationService()

        provider_input = (
            service.build_provider_input(
                organization=self.organization,
                lead=lead,
            )
        )

        self.assertIn(
            "preferred course",
            provider_input,
        )

        self.assertIn(
            "preferred batch timing",
            provider_input,
        )

        self.assertIn(
            "I want Security+",
            provider_input,
        )

    # ========================================================
    # CONVERSATION SUMMARY REFERENCE
    # ========================================================

    def test_provider_input_includes_conversation_summary(
        self,
    ):
        """
        The current Conversation Summary must be supplied to
        Qualification AI as supporting context.
        """

        lead = self.create_lead(
            "+919876543241"
        )

        self.create_message(
            lead=lead,
            external_id="qualification-summary-001",
            body=(
                "I manage around 20 leads a day and "
                "need help automating WhatsApp follow-up."
            ),
        )

        self.create_conversation_summary(
            lead=lead,
            summary=(
                "Lead manages approximately 20 leads daily "
                "and is interested in WhatsApp follow-up "
                "automation."
            ),
        )

        service = QualificationService()

        provider_input = (
            service.build_provider_input(
                organization=self.organization,
                lead=lead,
            )
        )

        self.assertIn(
            "CURRENT CONVERSATION SUMMARY",
            provider_input,
        )

        self.assertIn(
            "Lead manages approximately 20 leads daily",
            provider_input,
        )

        self.assertIn(
            "ACTUAL CONVERSATION",
            provider_input,
        )

        self.assertIn(
            "I manage around 20 leads a day",
            provider_input,
        )

    # ========================================================
    # INITIAL SYSTEM NOTE
    # ========================================================

    @patch(
        "apps.ai_engagement.services.qualification."
        "OpenAIProvider"
    )
    def test_generate_and_append_creates_initial_system_note(
        self,
        mocked_provider,
    ):
        """
        The first qualification result creates one cumulative
        AI system note.
        """

        lead = self.create_lead(
            "+919876543242"
        )

        self.create_message(
            lead=lead,
            external_id="qualification-note-001",
            body=(
                "I am interested in Security+ "
                "and need a weekend batch."
            ),
        )

        mocked_provider.return_value.generate_text.return_value = (
            AITextResult(
                text=(
                    "Lead is interested in Security+ and "
                    "prefers a weekend batch. Main training "
                    "goal is not yet fully confirmed."
                ),
                model="gpt-4.1-nano",
            )
        )

        service = QualificationService()

        note = service.generate_and_append(
            organization=self.organization,
            lead=lead,
        )

        self.assertIsNotNone(
            note
        )

        note.refresh_from_db()

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

        self.assertNotIn(
            "***** Updated Summary",
            note.note,
        )

        mocked_provider.return_value.generate_text.assert_called_once()

    # ========================================================
    # IDENTICAL RESULT
    # ========================================================

    @patch(
        "apps.ai_engagement.services.qualification."
        "OpenAIProvider"
    )
    def test_identical_summary_is_not_appended_again(
        self,
        mocked_provider,
    ):
        """
        An identical qualification result must not create a
        duplicate Updated Summary entry.
        """

        lead = self.create_lead(
            "+919876543243"
        )

        self.create_message(
            lead=lead,
            external_id="qualification-note-002",
            body=(
                "I want Security+ on weekends."
            ),
        )

        qualification_text = (
            "Lead is interested in Security+ and "
            "prefers a weekend batch."
        )

        mocked_provider.return_value.generate_text.return_value = (
            AITextResult(
                text=qualification_text,
                model="gpt-4.1-nano",
            )
        )

        service = QualificationService()

        first_note = (
            service.generate_and_append(
                organization=self.organization,
                lead=lead,
            )
        )

        second_note = (
            service.generate_and_append(
                organization=self.organization,
                lead=lead,
            )
        )

        self.assertIsNotNone(
            first_note
        )

        self.assertIsNone(
            second_note
        )

        first_note.refresh_from_db()

        self.assertEqual(
            first_note.note.count(
                "***** Updated Summary"
            ),
            0,
        )

        self.assertEqual(
            first_note.note.count(
                qualification_text
            ),
            1,
        )

        self.assertEqual(
            mocked_provider.return_value.generate_text.call_count,
            2,
        )

    # ========================================================
    # CHANGED RESULT
    # ========================================================

    @patch(
        "apps.ai_engagement.services.qualification."
        "OpenAIProvider"
    )
    def test_changed_summary_appends_updated_summary(
        self,
        mocked_provider,
    ):
        """
        A materially different qualification result must be
        appended to the existing AI system note.
        """

        lead = self.create_lead(
            "+919876543244"
        )

        self.create_message(
            lead=lead,
            external_id="qualification-note-003",
            body=(
                "I am considering Security+."
            ),
        )

        first_summary = (
            "Lead is considering Security+."
        )

        mocked_provider.return_value.generate_text.return_value = (
            AITextResult(
                text=first_summary,
                model="gpt-4.1-nano",
            )
        )

        service = QualificationService()

        note = service.generate_and_append(
            organization=self.organization,
            lead=lead,
        )

        self.assertIsNotNone(
            note
        )

        second_summary = (
            "Lead has confirmed Security+ as the "
            "preferred course and wants a weekend batch."
        )

        mocked_provider.return_value.generate_text.return_value = (
            AITextResult(
                text=second_summary,
                model="gpt-4.1-nano",
            )
        )

        updated_note = (
            service.generate_and_append(
                organization=self.organization,
                lead=lead,
            )
        )

        self.assertIsNotNone(
            updated_note
        )

        updated_note.refresh_from_db()

        self.assertEqual(
            updated_note.note.count(
                "***** Updated Summary"
            ),
            1,
        )

        self.assertIn(
            first_summary,
            updated_note.note,
        )

        self.assertIn(
            second_summary,
            updated_note.note,
        )

    # ========================================================
    # LATEST SUMMARY EXTRACTION
    # ========================================================

    def test_extract_latest_summary_from_history(
        self,
    ):
        """
        The service should correctly identify the most recent
        qualification result from cumulative note history.
        """

        service = QualificationService()

        note_text = (
            "<AI Qualification Summary - "
            "01 Sep 2026, 11:30 AM>\n"
            "Lead is considering Security+.\n\n"
            "***** Updated Summary 01/09 11:45 *****\n\n"
            "Lead confirmed Security+ and wants weekends.\n\n"
            "***** Updated Summary 01/09 12:05 *****\n\n"
            "Lead confirmed Security+ and requested a "
            "weekend batch."
        )

        latest = (
            service._extract_latest_summary(
                note_text
            )
        )

        self.assertEqual(
            latest,
            (
                "Lead confirmed Security+ and requested a "
                "weekend batch."
            ),
        )