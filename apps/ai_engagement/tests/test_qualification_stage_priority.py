"""Authored routing requests survive completion of the same qualification turn."""
from uuid import uuid4

from django.test import TestCase

from apps.ai_engagement.models import AIActionReceipt, OrgInfo
from apps.ai_engagement.services.engagement import EngagementDecision
from apps.ai_engagement.services.organization_profile import compile_qualification_requirements
from apps.ai_engagement.services.qualification_execution_contract import resolve_before_generation
from apps.ai_engagement.services.qualification_state import (
    persist_answer_updates, record_last_asked_requirement, state_for_lead,
)
from apps.ai_engagement.services import transactional_turn_runtime
from apps.ai_engagement.tests import test_engagement_controls as whatsapp_fixtures
from apps.channels.tests import test_instagram_ai as instagram_fixtures
from apps.crm.models import AttributeDefinition, Stage
from services.channels.instagram_ai import _apply_decision_state
from tests.playbook_fixtures import build_ai_playbook, qualification_questions


class _QualificationStagePriorityCases:
    def configure(self, *, authored_call=True):
        questions = (
            "[id: problem] What is your biggest problem?\nA. Slow replies\nB. Missed follow-ups\n\n"
            "[id: tool] Where do you manage leads?\nA. WhatsApp\nB. Excel/Google Sheets\n\n"
            "[id: volume] How many leads do you receive per day?\nA. 0-10\nB. 11-30\nC. More than 30\n\n"
            "[id: ads] Do you currently run paid ads?\nA. Yes\nB. No\nAll questions are required"
        )
        for name, key in (("Biggest Problem", "biggest_problem"), ("Lead Management Tool", "lead_management_tool"),
                          ("Leads/d", "leads_d"), ("Running Ads", "running_ads")):
            AttributeDefinition.objects.create(organization=self.organization, name=name, key=key,
                                               field_type=AttributeDefinition.FieldType.TEXT)
        self.call, _ = Stage.objects.get_or_create(
            pipeline=self.pipeline, name="Call Requested", defaults={"display_order": 80, "is_active": True},
        )
        info, _ = OrgInfo.objects.get_or_create(organization=self.organization)
        call_rule = (
            "\nWhen the customer explicitly requests a call or callback, move the lead to Call Requested "
            "in the current pipeline.\nAn explicit call request takes priority over qualification completion."
        ) if authored_call else ""
        info.ai_playbook = build_ai_playbook(questions=questions, rules=(
            "## Attribute mapped\nproblem -> Biggest Problem\ntool -> Lead Management Tool\n"
            "volume -> Leads/d\nads -> Running Ads\n\n## Stage shifting\n"
            "When all required qualification questions are answered, move to Qualified in the current pipeline."
            + call_rule + "\n\n## Reminder creation logic\nReminder 1:\n"
            "- Create when the customer explicitly requests a callback and provides or confirms a future date and time.\n"
            "- Title: Customer Callback."
        ))
        info.save()
        return compile_qualification_requirements(qualification_questions(info.ai_playbook))["requirements"]

    def decision(self, source, requirements, stage_id):
        return EngagementDecision(
            should_engage=True, message="Thanks for sharing your setup.", file_document_id=None,
            crm_actions=[{"type": "pipeline_transition", "stage_shift": {"stage_id": str(stage_id)}}],
            qualification_updates=[
                {"requirement_id": requirement["id"], "value": value,
                 "source_message_id": str(source.pk), "evidence": value}
                for requirement, value in zip(requirements, ("Slow replies", "WhatsApp", "11-30", "No"), strict=True)
            ],
            reason="NORMAL_CONVERSATION", reason_code="NORMAL_CONVERSATION", model="recorded-proposal",
        )

    def assert_completed(self, requirements, stage):
        self.lead.refresh_from_db()
        self.assertTrue(state_for_lead(self.lead, requirements=requirements)["qualification_completed"])
        self.assertEqual(self.lead.stage_id, stage.pk)
        self.assertEqual(self.lead.attributes["biggest_problem"], "Slow replies")
        self.assertEqual(self.lead.attributes["lead_management_tool"], "WhatsApp")
        self.assertEqual(self.lead.attributes["running_ads"], "No")
        self.assertEqual(AIActionReceipt.objects.filter(lead=self.lead, action_type="MOVE_STAGE").count(), 1)

    def test_all_four_answers_and_callback_preserve_authored_request_priority(self):
        requirements = self.configure()
        source = self.source("Slow replies; WhatsApp; 11-30; No. Please call me tomorrow at 3 PM.")
        self.resolve(source, self.decision(source, requirements, self.call.pk))
        self.assert_completed(requirements, self.call)
        self.assertEqual(self.lead.pipeline_id, self.pipeline.pk)

    def test_greeting_cannot_make_a_call_stage_proposal_override_completion(self):
        requirements = self.configure()
        source = self.source("Hello. Slow replies; WhatsApp; 11-30; No.")
        self.resolve(source, self.decision(source, requirements, self.call.pk))
        self.assert_completed(requirements, self.pipeline.stages.get(name="Qualified"))

    def test_unknown_stage_proposal_cannot_override_completion(self):
        requirements = self.configure()
        source = self.source("Slow replies; WhatsApp; 11-30; No. Please call me tomorrow at 3 PM.")
        self.resolve(source, self.decision(source, requirements, uuid4()))
        self.assert_completed(requirements, self.pipeline.stages.get(name="Qualified"))

    def test_unauthored_stage_proposal_cannot_override_completion(self):
        requirements = self.configure(authored_call=False)
        source = self.source("Slow replies; WhatsApp; 11-30; No. Please call me tomorrow at 3 PM.")
        self.resolve(source, self.decision(source, requirements, self.call.pk))
        self.assert_completed(requirements, self.pipeline.stages.get(name="Qualified"))

    def test_negated_callback_cannot_override_completion(self):
        requirements = self.configure()
        source = self.source("Slow replies; WhatsApp; 11-30; No. Do not call me tomorrow at 3 PM.")
        self.resolve(source, self.decision(source, requirements, self.call.pk))
        self.assert_completed(requirements, self.pipeline.stages.get(name="Qualified"))


class WhatsAppQualificationStagePriorityTests(_QualificationStagePriorityCases, TestCase):
    setUp = whatsapp_fixtures.AIEngagementControlTests.setUp
    _inbound = whatsapp_fixtures.AIEngagementControlTests._inbound

    def source(self, body):
        source = self._inbound()
        source.body = body
        source.save(update_fields=["body"])
        return source

    def resolve(self, source, decision):
        return transactional_turn_runtime._resolve_state_before_response(
            organization=self.organization, lead=self.lead, source_message_id=source.pk,
            decision=decision, account_id=self.account.pk,
        )

    def test_mixed_last_answer_and_callback_defers_pre_generation_completion(self):
        requirements = self.configure()
        earlier = self._inbound("priority-earlier-answers")
        earlier.body = "Slow replies; WhatsApp; 11-30"
        earlier.save(update_fields=["body"])
        persist_answer_updates(lead=self.lead, updates=[
            {"requirement_id": requirement["id"], "value": value,
             "source_message_id": str(earlier.pk), "evidence": value}
            for requirement, value in zip(requirements[:3], ("Slow replies", "WhatsApp", "11-30"), strict=True)
        ])
        record_last_asked_requirement(self.lead, requirements[-1]["id"], requirements=requirements)
        source = self.source("No. Please call me tomorrow at 3 PM.")
        result = resolve_before_generation(
            organization=self.organization, lead=self.lead, source_message_id=source.pk,
            account_id=self.account.pk,
        )
        self.assertEqual(result, {"applied": False, "reason": "requires_llm_interpretation"})
        self.lead.refresh_from_db()
        source.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.new_lead.pk)
        self.assertFalse(AIActionReceipt.objects.filter(lead=self.lead).exists())
        self.assertFalse(source.raw_payload.get("shvya_ai_processing", {}).get("state_resolved"))


class InstagramQualificationStagePriorityTests(_QualificationStagePriorityCases, TestCase):
    def setUp(self):
        instagram_fixtures.InstagramAIEngagementTests.setUp(self)
        self.organization = self.org

    def source(self, body):
        self.inbound.body = body
        self.inbound.save(update_fields=["body"])
        return self.inbound

    def resolve(self, source, decision):
        return _apply_decision_state(
            organization=self.organization, lead=self.lead, source=source,
            decision=decision, finalize=False,
        )
