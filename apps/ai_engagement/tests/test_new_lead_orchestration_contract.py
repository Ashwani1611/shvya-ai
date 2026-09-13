from __future__ import annotations

from types import SimpleNamespace

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase

from apps.ai_engagement.graph.policy_actions import build_controlled_actions
from apps.ai_engagement.models import Document, OrgInfo
from apps.ai_engagement.services.context import AIContext
from apps.ai_engagement.services.crm_executor import CRMActionExecutor
from apps.ai_engagement.services.file_sharing import FileSharingService
from apps.ai_engagement.services.organization_profile import compile_qualification_requirements
from apps.ai_engagement.services.qualification_state import (
    MODE_CONVERSATION,
    MODE_QUALIFICATION,
    RESULT_QUALIFIED,
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    apply_unambiguous_reply,
    record_last_asked_requirement,
    state_for_lead,
)
from apps.crm.models import Lead, LeadNote, Pipeline, Stage
from apps.organizations.models import Organization


class NewLeadQualificationLifecycleTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Qualification Scope Org")
        self.pipeline = Pipeline.objects.create(organization=self.organization, name="Sales", is_active=True)
        self.new_lead = self.pipeline.stages.get(name="New leads")
        self.qualified = self.pipeline.stages.get(name="Qualified")
        self.follow_up = Stage.objects.create(
            pipeline=self.pipeline,
            name="Follow-up Test",
            display_order=700,
            is_active=True,
        )
        self.org_info = OrgInfo.objects.create(
            organization=self.organization,
            about="Acme manages existing sales leads.",
            bot_languages="English, Hindi",
            qualification_requirements=(
                "What is your budget?\nA. Under 50k\nB. 50k+\n"
                "Which city are you in?\nA. Delhi\nB. Mumbai"
            ),
            engagement_instructions="Be concise and consultative.",
            ai_enabled=True,
        )
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.new_lead,
            name="Lead",
            phone="+919999990001",
        )

    def _requirements(self):
        return compile_qualification_requirements(self.org_info.qualification_requirements)["requirements"]

    def test_incomplete_qualification_pauses_outside_new_lead_and_resumes_same_requirement(self):
        requirements = self._requirements()
        record_last_asked_requirement(self.lead, requirements[0]["id"], requirements=requirements)
        apply_unambiguous_reply(
            lead=self.lead,
            requirements=requirements,
            text="A",
            source_message_id="source-1",
        )
        self.lead.refresh_from_db()
        before = state_for_lead(self.lead, requirements=requirements)
        self.assertEqual(before["qualification_status"], STATUS_IN_PROGRESS)
        self.assertEqual(before["engagement_mode"], MODE_QUALIFICATION)
        pending_id = before["current_requirement_id"]

        self.lead.stage = self.follow_up
        self.lead.save(update_fields=["stage", "updated_at"])
        self.lead.refresh_from_db()
        paused = state_for_lead(self.lead, requirements=requirements)
        self.assertEqual(paused["qualification_status"], STATUS_IN_PROGRESS)
        self.assertEqual(paused["engagement_mode"], MODE_CONVERSATION)
        self.assertEqual(paused["current_requirement_id"], pending_id)

        self.lead.stage = self.new_lead
        self.lead.save(update_fields=["stage", "updated_at"])
        self.lead.refresh_from_db()
        resumed = state_for_lead(self.lead, requirements=requirements)
        self.assertEqual(resumed["qualification_status"], STATUS_IN_PROGRESS)
        self.assertEqual(resumed["engagement_mode"], MODE_QUALIFICATION)
        self.assertEqual(resumed["current_requirement_id"], pending_id)

    def test_final_answer_then_qualified_transition_sets_result_and_short_note(self):
        self.org_info.qualification_requirements = "What is your budget?\nA. Under 50k\nB. 50k+"
        self.org_info.save(update_fields=["qualification_requirements", "updated_at"])
        requirements = self._requirements()
        record_last_asked_requirement(self.lead, requirements[0]["id"], requirements=requirements)
        apply_unambiguous_reply(
            lead=self.lead,
            requirements=requirements,
            text="B",
            source_message_id="source-final",
        )
        self.lead.refresh_from_db()
        answered = state_for_lead(self.lead, requirements=requirements)
        self.assertEqual(answered["qualification_status"], STATUS_COMPLETED)
        self.assertEqual(answered["engagement_mode"], MODE_CONVERSATION)

        result = CRMActionExecutor().execute(
            organization=self.organization,
            lead=self.lead,
            actions=[{"type": "pipeline_transition", "stage_shift": {"stage_id": str(self.qualified.id)}}],
        )
        self.lead.refresh_from_db()
        final_state = state_for_lead(self.lead, requirements=requirements)
        self.assertEqual(self.lead.stage_id, self.qualified.id)
        self.assertEqual(final_state["qualification_result"], RESULT_QUALIFIED)
        note = LeadNote.objects.get(
            lead=self.lead,
            note_type="system",
            note__startswith="<AI Qualification Summary",
        )
        self.assertIn("50k+", note.note)
        self.assertLessEqual(len(note.note), 500)
        self.assertEqual(result[0]["qualification_note_id"], str(note.id))

    def test_ai_brain_org_fields_are_runtime_source_of_truth(self):
        from apps.ai_engagement.services.context import AIContextBuilder
        context = AIContextBuilder()._build_organization_context(organization=self.organization)
        self.assertEqual(context["about"], self.org_info.about)
        self.assertEqual(context["bot_languages"], self.org_info.bot_languages)
        self.assertEqual(context["engagement_instructions"], self.org_info.engagement_instructions)


class QualificationStagePolicyTests(SimpleTestCase):
    def test_qualified_transition_is_blocked_in_conversation_mode(self):
        context = SimpleNamespace(
            conversation={"messages": [{"id": "m1", "direction": "inbound", "body": "My budget is 75k"}]},
            pipeline={"attribute_definitions": []},
        )
        state = {
            "qualification_status": "in_progress",
            "engagement_mode": MODE_CONVERSATION,
            "requirement_states": {"budget": {"status": "unknown", "value": None, "source_message_id": None}},
            "qualified_stage_id": "qualified-id",
        }
        decision = SimpleNamespace(
            qualification_updates=[{
                "requirement_id": "budget",
                "value": "75k",
                "source_message_id": "m1",
                "evidence": "75k",
            }],
            crm_actions=[],
        )
        actions, result = build_controlled_actions(
            decision=decision,
            context=context,
            runtime_policy={"qualification": {"criteria": [{"id": "budget", "required": True, "pass_condition": None}]}},
            qualification_state=state,
            requirements=[{"id": "budget", "required": True}],
        )
        self.assertFalse(any(item["type"] == "pipeline_transition" for item in actions))
        self.assertEqual(result["evaluation"]["outcome"], "in_progress")

    def test_qualification_answer_maps_to_matching_named_attribute(self):
        context = SimpleNamespace(
            conversation={"messages": [{"id": "m1", "direction": "inbound", "body": "Slow replies"}]},
            pipeline={"attribute_definitions": [{"key": "biggest_challenge", "name": "Biggest challenge", "field_type": "text"}]},
        )
        state = {
            "qualification_status": "in_progress",
            "engagement_mode": MODE_QUALIFICATION,
            "requirement_states": {"challenge": {"status": "unknown", "value": None}},
            "qualified_stage_id": None,
        }
        update = {
            "requirement_id": "challenge",
            "value": "Slow replies",
            "source_message_id": "m1",
            "evidence": "Slow replies",
        }
        actions, _ = build_controlled_actions(
            decision=SimpleNamespace(qualification_updates=[update], crm_actions=[]),
            context=context,
            runtime_policy={"qualification": {"criteria": [{"id": "challenge", "label": "What is your biggest challenge with managing leads", "required": True}]}},
            qualification_state=state,
            requirements=[{"id": "challenge", "required": True}],
        )
        attribute = next(item for item in actions if item["type"] == "attribute_updates")
        self.assertEqual(attribute["updates"], [{"key": "biggest_challenge", "value": "Slow replies"}])

    def test_grounded_reminder_is_kept_without_qualification(self):
        action = {
            "type": "create_reminder",
            "title": "Call lead",
            "description": "Call tomorrow at 6pm",
            "due_at": "2026-09-14T18:00:00+05:30",
        }
        context = SimpleNamespace(
            conversation={"messages": [{"id": "m1", "direction": "inbound", "body": "Please call me tomorrow at 6pm"}]},
            pipeline={"attribute_definitions": []},
        )
        actions, _ = build_controlled_actions(
            decision=SimpleNamespace(qualification_updates=[], crm_actions=[action]),
            context=context,
            runtime_policy={"qualification": {"criteria": []}},
            qualification_state={"engagement_mode": MODE_CONVERSATION, "requirement_states": {}},
            requirements=[],
        )
        self.assertEqual(actions, [action])


class ExplicitGuidedFileCandidateTests(TestCase):
    def test_explicit_file_request_exposes_configured_file_without_rag_chunk(self):
        organization = Organization.objects.create(name="Files Org")
        pipeline = Pipeline.objects.create(organization=organization, name="Sales")
        stage = pipeline.stages.get(name="New leads")
        lead = Lead.objects.create(
            organization=organization,
            pipeline=pipeline,
            stage=stage,
            name="File Lead",
            phone="+919999990099",
        )
        document = Document.objects.create(
            organization=organization,
            name="SHVYA Brochure",
            source_key="shvya-brochure",
            version=1,
            file=SimpleUploadedFile("brochure.pdf", b"brochure", content_type="application/pdf"),
            processing_status=Document.ProcessingStatus.COMPLETED,
            is_active=True,
            share_instruction="Share this brochure when a lead asks for the brochure or company deck.",
        )
        context = AIContext(
            organization={"id": str(organization.id), "name": organization.name},
            lead={"id": str(lead.id)},
            pipeline={},
            stage={},
            contacts=[],
            attributes=[],
            conversation={
                "message_count": 1,
                "messages": [{"id": "m1", "direction": "inbound", "body": "Please send me your brochure"}],
            },
            conversation_summary=None,
            qualification_notes=[],
            knowledge=[],
        )
        candidates = FileSharingService().build_file_candidates(organization=organization, context=context)
        self.assertEqual([item["document_id"] for item in candidates], [document.id])
        context.conversation["messages"][0]["body"] = "Thanks"
        self.assertEqual(FileSharingService().build_file_candidates(organization=organization, context=context), [])
