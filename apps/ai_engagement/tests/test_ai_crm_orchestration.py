from django.test import TestCase

from apps.ai_engagement.models import InternalConversationSummary
from apps.ai_engagement.services.context import AIContextBuilder
from apps.ai_engagement.services.crm_executor import CRMActionExecutor
from apps.ai_engagement.summary_note_signals import CONVERSATION_SUMMARY_HEADER
from apps.crm.models import Lead, LeadNote, Pipeline
from apps.organizations.models import Organization


class AICRMOrchestrationTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Acme")
        self.sales = Pipeline.objects.create(
            organization=self.organization,
            name="Sales",
            country_code="+91",
            phone_number="9876543210",
        )
        self.support = Pipeline.objects.create(
            organization=self.organization,
            name="Support",
            country_code="+91",
            phone_number="8888888888",
        )
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.sales,
            stage=self.sales.stages.get(name="New leads"),
            name="Test Lead",
            phone="+919111111111",
        )

    def test_context_exposes_active_stages_from_other_org_owned_pipelines(self):
        context = AIContextBuilder()._build_pipeline_context(lead=self.lead)
        support_stage = self.support.stages.get(name="New leads")

        candidate = next(
            item
            for item in context["available_stages"]
            if item["id"] == str(support_stage.id)
        )
        self.assertEqual(candidate["pipeline_id"], str(self.support.id))
        self.assertEqual(candidate["pipeline_name"], "Support")
        self.assertFalse(candidate["is_current_pipeline"])

    def test_executor_moves_lead_to_stage_in_another_org_owned_pipeline(self):
        target = self.support.stages.get(name="New leads")

        result = CRMActionExecutor().execute(
            organization=self.organization,
            lead=self.lead,
            actions=[
                {
                    "type": "pipeline_transition",
                    "stage_shift": {"stage_id": str(target.id)},
                }
            ],
        )

        self.lead.refresh_from_db()
        self.assertEqual(self.lead.pipeline_id, self.support.id)
        self.assertEqual(self.lead.stage_id, target.id)
        self.assertEqual(result[0]["status"], "executed")
        self.assertEqual(result[0]["pipeline_id"], str(self.support.id))

    def test_executor_rejects_stage_from_another_organization(self):
        other_org = Organization.objects.create(name="Other")
        other_pipeline = Pipeline.objects.create(
            organization=other_org,
            name="Other Sales",
        )
        target = other_pipeline.stages.get(name="New leads")

        with self.assertRaisesMessage(
            Exception,
            "Requested stage does not belong to an active pipeline in this organization.",
        ):
            CRMActionExecutor().execute(
                organization=self.organization,
                lead=self.lead,
                actions=[
                    {
                        "type": "pipeline_transition",
                        "stage_shift": {"stage_id": str(target.id)},
                    }
                ],
            )

    def test_latest_conversation_summary_is_mirrored_into_one_system_note(self):
        InternalConversationSummary.objects.create(
            organization=self.organization,
            lead=self.lead,
            summary="Lead asked about pricing and wants a callback tomorrow.",
            source_message_count=2,
            is_active=True,
        )

        note = LeadNote.objects.get(
            lead=self.lead,
            note_type="system",
            note__startswith=CONVERSATION_SUMMARY_HEADER,
        )
        self.assertIn("pricing", note.note)

        InternalConversationSummary.objects.create(
            organization=self.organization,
            lead=self.lead,
            summary="Lead confirmed budget and still wants a callback tomorrow.",
            source_message_count=4,
            is_active=True,
        )

        notes = LeadNote.objects.filter(
            lead=self.lead,
            note_type="system",
            note__startswith=CONVERSATION_SUMMARY_HEADER,
        )
        self.assertEqual(notes.count(), 1)
        self.assertIn("confirmed budget", notes.get().note)