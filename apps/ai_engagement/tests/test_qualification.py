from __future__ import annotations

from unittest.mock import patch
import json
from datetime import timedelta
from apps.ai_engagement.services.qualification import QualificationError
from apps.ai_engagement.services.internal_summary import InternalSummaryService

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

    def setup_answer(self):
        OrgInfo.objects.filter(organization=self.organization).update(qualification_requirements="Which course?")
        lead = self.create_lead("+919876543240")
        self.create_message(lead=lead, external_id="question", body="Which course?", direction="outbound")
        answer = self.create_message(lead=lead, external_id="answer", body="Security+")
        service = QualificationService()
        source = json.loads(service.build_provider_input(organization=self.organization, lead=lead))
        evidence = {"answers": [{"requirement_id": source["requirements"][0]["id"],
                     "message_id": str(answer.id), "question_quote": "Which course?", "quote": "Security+"}]}
        return lead, service, evidence

    @patch("apps.ai_engagement.services.qualification.OpenAIProvider")
    def test_only_evidenced_answers_are_saved_and_deduplicated(self, provider):
        lead, service, evidence = self.setup_answer()
        provider.return_value.generate_text.return_value = AITextResult(text=json.dumps(evidence), model="test")
        note = service.generate_and_append(organization=self.organization, lead=lead)
        self.assertIn("Security+", note.note)
        self.assertLessEqual(len(note.note), 500)
        self.assertIsNone(service.generate_and_append(organization=self.organization, lead=lead))

    @patch("apps.ai_engagement.services.qualification.OpenAIProvider")
    def test_invented_answer_is_rejected(self, provider):
        lead, service, evidence = self.setup_answer()
        evidence["answers"][0]["quote"] = "Unlimited budget"
        provider.return_value.generate_text.return_value = AITextResult(text=json.dumps(evidence), model="test")
        with self.assertRaises(QualificationError):
            service.generate_and_append(organization=self.organization, lead=lead)
        self.assertIsNone(service.get_current_ai_note(lead=lead))

    def test_input_excludes_old_chats_and_unrelated_summary(self):
        lead, service, _ = self.setup_answer()
        self.create_conversation_summary(lead=lead, summary="Invented old qualification")
        old = self.create_message(lead=lead, external_id="old", body="Old chat must not leak")
        WhatsAppMessage.objects.filter(pk=old.pk).update(created_at=lead.created_at - timedelta(days=1))
        payload = service.build_provider_input(organization=self.organization, lead=lead)
        self.assertNotIn("Invented old qualification", payload)
        self.assertNotIn("Old chat must not leak", payload)
        self.assertIn("Security+", payload)

    def test_note_updates_stay_bounded(self):
        lead, service, _ = self.setup_answer()
        note = service.append_summary(lead=lead, summary="a" * 600, model="test")
        self.assertLessEqual(len(note.note), 500)
        for index in range(10):
            note = service.append_summary(lead=lead, summary=f"Update {index} " + "b" * 200, model="test")
            self.assertLessEqual(len(note.note), 500)
        self.assertIn("Update 9", note.note)

    def test_lead_creation_message_is_included_even_with_earlier_event_time(self):
        lead, _, _ = self.setup_answer()
        trigger = self.create_message(lead=lead, external_id="trigger", body="Create my lead")
        WhatsAppMessage.objects.filter(pk=trigger.pk).update(
            created_at=lead.created_at - timedelta(seconds=1), raw_payload={"leadCreationMessage": True})
        messages = InternalSummaryService().get_messages(organization=self.organization, lead=lead)
        self.assertIn(trigger.id, [m.id for m in messages])

