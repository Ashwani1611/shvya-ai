from types import SimpleNamespace

from django.test import TestCase

from apps.accounts.models import User
from apps.ai_engagement.models import Document
from apps.ai_engagement.services.action_planner import (
    ActionPlanner,
    STATUS_ACCEPTED,
    STATUS_REJECTED,
)
from apps.ai_engagement.services.crm_executor import CRMActionExecutor
from apps.crm.models import (
    AttributeDefinition,
    Lead,
    LeadContact,
    LeadNote,
    LeadReminder,
    Pipeline,
    Stage,
)
from apps.organizations.models import Organization


class ActionPlannerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name="Phase 7 Org")
        cls.other_org = Organization.objects.create(name="Other Phase 7 Org")
        cls.owner = User.objects.create_user(
            email="phase7@example.com",
            password="password",
            name="Phase 7 Owner",
            organization=cls.org,
        )
        cls.pipeline = Pipeline.objects.create(
            organization=cls.org,
            name="Sales",
            description="",
            owner=cls.owner,
            is_active=True,
        )
        cls.stages = list(
            Stage.objects.filter(pipeline=cls.pipeline, is_active=True).order_by("display_order")
        )
        if len(cls.stages) < 2:
            raise AssertionError("Expected default pipeline stages.")
        cls.stage_one = cls.stages[0]
        cls.stage_two = cls.stages[1]
        cls.other_pipeline = Pipeline.objects.create(
            organization=cls.other_org,
            name="Other",
            description="",
            is_active=True,
        )
        cls.other_stage = Stage.objects.filter(
            pipeline=cls.other_pipeline,
            is_active=True,
        ).order_by("display_order").first()

    def setUp(self):
        self.lead = Lead.objects.create(
            organization=self.org,
            pipeline=self.pipeline,
            stage=self.stage_one,
            name="Planner Lead",
            phone="+919999999901",
            email="planner@example.com",
            notes="",
            attributes={},
        )
        self.planner = ActionPlanner()
        self.source = SimpleNamespace(
            id="source-1",
            body="We receive around 30 leads daily. Please call me tomorrow.",
        )

    def decision(self, *, actions=None, file_document_id=None, reason_code="NORMAL_CONVERSATION"):
        return SimpleNamespace(
            crm_actions=list(actions or []),
            file_document_id=file_document_id,
            reason_code=reason_code,
        )

    def plan(self, decision):
        return self.planner.plan(
            organization=self.org,
            lead=self.lead,
            decision=decision,
            source_message=self.source,
            source_intent="QUALIFICATION_ANSWER+CALL_REQUEST",
            source_policy="continue",
            policy_outcome="continue",
        )

    def test_valid_attribute_update_proposal(self):
        AttributeDefinition.objects.create(
            organization=self.org,
            name="Lead Volume",
            key="lead_volume",
            field_type="number",
            description="",
            options=[],
            display_order=1,
        )
        plan = self.plan(self.decision(actions=[{
            "type": "attribute_updates",
            "updates": [{"key": "lead_volume", "value": 30}],
        }]))
        proposal = plan.actions[0]
        self.assertEqual(proposal.validation_status, STATUS_ACCEPTED)
        self.assertEqual(proposal.action_type, "UPDATE_ATTRIBUTE")
        self.assertEqual(proposal.source_evidence[0]["message_id"], "source-1")
        self.assertEqual(plan.executor_actions[0]["updates"][0]["value"], 30)

    def test_unknown_attribute_rejected(self):
        plan = self.plan(self.decision(actions=[{
            "type": "attribute_updates",
            "updates": [{"key": "missing", "value": "x"}],
        }]))
        self.assertEqual(plan.actions[0].validation_status, STATUS_REJECTED)
        self.assertEqual(plan.executor_actions, [])

    def test_cross_org_attribute_rejected(self):
        AttributeDefinition.objects.create(
            organization=self.other_org,
            name="Foreign",
            key="foreign_key",
            field_type="text",
            description="",
            options=[],
            display_order=1,
        )
        plan = self.plan(self.decision(actions=[{
            "type": "attribute_updates",
            "updates": [{"key": "foreign_key", "value": "x"}],
        }]))
        self.assertEqual(plan.actions[0].validation_status, STATUS_REJECTED)

    def test_valid_stage_transition_proposal(self):
        plan = self.plan(self.decision(actions=[{
            "type": "pipeline_transition",
            "stage_shift": {"stage_id": str(self.stage_two.id)},
        }]))
        self.assertEqual(plan.actions[0].validation_status, STATUS_ACCEPTED)
        self.assertEqual(plan.actions[0].action_type, "MOVE_STAGE")

    def test_cross_org_stage_rejected(self):
        plan = self.plan(self.decision(actions=[{
            "type": "pipeline_transition",
            "stage_shift": {"stage_id": str(self.other_stage.id)},
        }]))
        self.assertEqual(plan.actions[0].validation_status, STATUS_REJECTED)

    def test_inactive_stage_rejected(self):
        inactive_stage = Stage.objects.filter(
            pipeline=self.pipeline,
            name="Nurturing",
        ).first()
        if inactive_stage is None:
            inactive_stage = Stage.objects.create(
                pipeline=self.pipeline,
                name="Planner Inactive",
                display_order=90,
                is_active=True,
            )
        inactive_stage.is_active = False
        inactive_stage.save(update_fields=["is_active"])
        inactive_stage.refresh_from_db()
        self.assertFalse(inactive_stage.is_active)
        plan = self.plan(self.decision(actions=[{
            "type": "pipeline_transition",
            "stage_shift": {"stage_id": str(inactive_stage.id)},
        }]))
        self.assertEqual(plan.actions[0].validation_status, STATUS_REJECTED)

    def test_add_note_is_proposal_only(self):
        before = LeadNote.objects.filter(lead=self.lead).count()
        plan = self.plan(self.decision(actions=[{
            "type": "add_note",
            "note": "Customer requested follow-up.",
        }]))
        self.assertEqual(plan.actions[0].validation_status, STATUS_ACCEPTED)
        self.assertEqual(LeadNote.objects.filter(lead=self.lead).count(), before)

    def test_oversized_note_rejected(self):
        plan = self.plan(self.decision(actions=[{"type": "add_note", "note": "x" * 5001}]))
        self.assertEqual(plan.actions[0].validation_status, STATUS_REJECTED)

    def test_create_reminder_is_proposal_only(self):
        before = LeadReminder.objects.filter(lead=self.lead).count()
        plan = self.plan(self.decision(actions=[{
            "type": "create_reminder",
            "title": "Call customer",
            "description": "Customer explicitly requested a call.",
            "due_at": "2026-09-18T10:00:00+05:30",
        }]))
        self.assertEqual(plan.actions[0].validation_status, STATUS_ACCEPTED)
        self.assertEqual(LeadReminder.objects.filter(lead=self.lead).count(), before)

    def test_ambiguous_reminder_time_is_rejected_by_schema(self):
        plan = self.plan(self.decision(actions=[{
            "type": "create_reminder",
            "title": "Call customer",
            "description": "",
            "due_at": "tomorrow",
        }]))
        self.assertEqual(plan.actions[0].validation_status, STATUS_REJECTED)
        self.assertEqual(plan.executor_actions, [])

    def test_valid_contact_update(self):
        contact = LeadContact.objects.create(lead=self.lead, channel="phone", handle="123")
        plan = self.plan(self.decision(actions=[{
            "type": "contact_updates",
            "updates": [{
                "contact_id": str(contact.id),
                "channel": "whatsapp",
                "handle": "+919999999901",
            }],
        }]))
        self.assertEqual(plan.actions[0].validation_status, STATUS_ACCEPTED)
        contact.refresh_from_db()
        self.assertEqual(contact.channel, "phone")

    def test_foreign_contact_rejected(self):
        foreign_lead = Lead.objects.create(
            organization=self.org,
            pipeline=self.pipeline,
            stage=self.stage_one,
            name="Other Lead",
            phone="+919999999902",
            email="otherlead@example.com",
            attributes={},
        )
        contact = LeadContact.objects.create(lead=foreign_lead, channel="phone", handle="123")
        plan = self.plan(self.decision(actions=[{
            "type": "contact_updates",
            "updates": [{
                "contact_id": str(contact.id),
                "channel": "phone",
                "handle": "456",
            }],
        }]))
        self.assertEqual(plan.actions[0].validation_status, STATUS_REJECTED)

    def test_valid_send_file_proposal(self):
        document = Document.objects.create(
            organization=self.org,
            name="Brochure",
            source_key="brochure.pdf",
            version=1,
            file="ai_knowledge/brochure.pdf",
            share_instruction="Send when the customer requests the brochure.",
            processing_status=Document.ProcessingStatus.COMPLETED,
            is_active=True,
        )
        plan = self.plan(self.decision(file_document_id=document.id))
        proposal = plan.actions[0]
        self.assertEqual(proposal.action_type, "SEND_FILE")
        self.assertEqual(proposal.validation_status, STATUS_ACCEPTED)
        self.assertEqual(plan.executor_actions, [])

    def test_foreign_file_rejected(self):
        document = Document.objects.create(
            organization=self.other_org,
            name="Foreign Brochure",
            source_key="foreign.pdf",
            version=1,
            file="ai_knowledge/foreign.pdf",
            share_instruction="share",
            processing_status=Document.ProcessingStatus.COMPLETED,
            is_active=True,
        )
        plan = self.plan(self.decision(file_document_id=document.id))
        self.assertEqual(plan.actions[0].validation_status, STATUS_REJECTED)

    def test_unshareable_file_rejected(self):
        document = Document.objects.create(
            organization=self.org,
            name="Private",
            source_key="private.pdf",
            version=1,
            file="ai_knowledge/private.pdf",
            share_instruction="",
            processing_status=Document.ProcessingStatus.COMPLETED,
            is_active=True,
        )
        plan = self.plan(self.decision(file_document_id=document.id))
        self.assertEqual(plan.actions[0].validation_status, STATUS_REJECTED)

    def test_human_handoff_is_non_executing_proposal(self):
        plan = self.plan(self.decision(reason_code="HUMAN_HANDOFF"))
        self.assertEqual(plan.actions[0].action_type, "HUMAN_HANDOFF")
        self.assertEqual(plan.actions[0].validation_status, STATUS_ACCEPTED)
        self.assertEqual(plan.executor_actions, [])

    def test_unsupported_action_type_rejected(self):
        plan = self.plan(self.decision(actions=[{"type": "BOOK_GYM_TRIAL"}]))
        self.assertEqual(plan.actions[0].validation_status, STATUS_REJECTED)

    def test_planner_performs_zero_direct_lead_mutation(self):
        original_stage = self.lead.stage_id
        original_attributes = dict(self.lead.attributes)
        self.plan(self.decision(actions=[{
            "type": "pipeline_transition",
            "stage_shift": {"stage_id": str(self.stage_two.id)},
        }]))
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, original_stage)
        self.assertEqual(self.lead.attributes, original_attributes)

    def test_same_source_action_has_stable_idempotency_key(self):
        action = {"type": "add_note", "note": "Stable proposal"}
        first = self.plan(self.decision(actions=[action])).actions[0]
        second = self.plan(self.decision(actions=[action])).actions[0]
        self.assertEqual(first.idempotency_key, second.idempotency_key)

    def test_executor_still_owns_mutation_after_planning(self):
        plan = self.plan(self.decision(actions=[{
            "type": "pipeline_transition",
            "stage_shift": {"stage_id": str(self.stage_two.id)},
        }]))
        self.assertEqual(self.lead.stage_id, self.stage_one.id)
        result = CRMActionExecutor().execute(
            organization=self.org,
            lead=self.lead,
            actions=plan.executor_actions,
        )
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage_id, self.stage_two.id)
        self.assertEqual(result[0]["status"], "executed")

    def test_tenant_mismatch_fails_closed(self):
        foreign_lead = Lead.objects.create(
            organization=self.other_org,
            pipeline=self.other_pipeline,
            stage=self.other_stage,
            name="Foreign",
            phone="+919999999903",
            email="foreign@example.com",
            attributes={},
        )
        with self.assertRaises(Exception):
            self.planner.plan(
                organization=self.org,
                lead=foreign_lead,
                decision=self.decision(),
                source_message=self.source,
            )

    def test_no_additional_model_call_is_required(self):
        plan = self.plan(self.decision(actions=[{
            "type": "add_note",
            "note": "No model needed",
        }]))
        self.assertEqual(plan.actions[0].validation_status, STATUS_ACCEPTED)
        self.assertEqual(plan.plan_version, "phase7.v2")
