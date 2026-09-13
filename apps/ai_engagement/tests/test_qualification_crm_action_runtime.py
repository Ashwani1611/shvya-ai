from __future__ import annotations

from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.ai_engagement.graph.policy_actions import build_controlled_actions
from apps.ai_engagement.graph.runtime_policy import get_runtime_policy
from apps.ai_engagement.models import OrgInfo
from apps.ai_engagement.services.context import AIContextBuilder
from apps.ai_engagement.services.crm_executor import CRMActionExecutor
from apps.ai_engagement.services.organization_profile import (
    compile_org_ai_profile_from_context,
    compile_qualification_requirements,
)
from apps.ai_engagement.services.qualification_state import (
    MODE_CONVERSATION,
    apply_unambiguous_reply,
    record_last_asked_requirement,
    state_for_lead,
)
from apps.crm.models import AttributeDefinition, Lead, Pipeline, Stage
from apps.organizations.models import Organization


class PersistedQualificationCRMActionTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="CRM Action Runtime Org")
        self.pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Sales",
            is_active=True,
        )
        self.new_lead = self.pipeline.stages.get(name="New leads")
        self.qualified = self.pipeline.stages.get(name="Qualified")
        self.org_info = OrgInfo.objects.create(
            organization=self.organization,
            qualification_requirements=(
                "How many leads do you typically receive per day?\n"
                "A. 0-10\n"
                "B. 10-30\n"
                "C. 30+"
            ),
            bot_languages="English",
            engagement_instructions="Be concise.",
            ai_enabled=True,
        )
        AttributeDefinition.objects.create(
            organization=self.organization,
            name="Daily Leads",
            key="daily_leads",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.new_lead,
            name="Qualification Lead",
            phone="+919999991234",
        )

    def test_backend_captured_final_answer_fills_attribute_and_moves_qualified(self):
        requirements = compile_qualification_requirements(
            self.org_info.qualification_requirements
        )["requirements"]
        requirement = requirements[0]
        record_last_asked_requirement(
            self.lead,
            requirement["id"],
            requirements=requirements,
        )

        direct = apply_unambiguous_reply(
            lead=self.lead,
            requirements=requirements,
            text="B",
            source_message_id="inbound-final",
        )
        self.assertTrue(direct["changed"])
        self.assertEqual(direct["state"]["qualification_status"], "completed")
        self.assertEqual(direct["state"]["engagement_mode"], MODE_CONVERSATION)

        self.lead.refresh_from_db()
        context = AIContextBuilder().build(
            organization=self.organization,
            lead=self.lead,
            message_limit=12,
            knowledge_limit=3,
            note_limit=5,
        )
        context.conversation["messages"] = [{
            "id": "inbound-final",
            "direction": "inbound",
            "body": "B",
        }]
        profile = compile_org_ai_profile_from_context(context.organization)
        runtime_policy = get_runtime_policy(
            organization=self.organization,
            profile=profile,
        )
        qualification_state = state_for_lead(self.lead, requirements=requirements)
        decision = SimpleNamespace(
            qualification_updates=[],
            crm_actions=[],
        )

        actions, result = build_controlled_actions(
            decision=decision,
            context=context,
            runtime_policy=runtime_policy,
            qualification_state=qualification_state,
            requirements=requirements,
        )

        attribute_action = next(
            item for item in actions if item.get("type") == "attribute_updates"
        )
        self.assertEqual(
            attribute_action["updates"],
            [{"key": "daily_leads", "value": "10-30"}],
        )
        transition = next(
            item for item in actions if item.get("type") == "pipeline_transition"
        )
        self.assertEqual(transition["stage_shift"]["stage_id"], str(self.qualified.id))
        self.assertEqual(result["evaluation"]["outcome"], "qualified")

        CRMActionExecutor().execute(
            organization=self.organization,
            lead=self.lead,
            actions=actions,
        )
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.attributes["daily_leads"], "10-30")
        self.assertEqual(self.lead.stage_id, self.qualified.id)
        final_state = state_for_lead(self.lead, requirements=requirements)
        self.assertEqual(final_state["qualification_result"], "qualified")


class ConversationCRMActionRuntimeTests(SimpleTestCase):
    def _context(self, *, stage_name: str, latest_text: str, available_stages=None):
        return SimpleNamespace(
            stage={"id": "current-stage", "name": stage_name},
            pipeline={
                "attribute_definitions": [],
                "available_stages": available_stages or [],
            },
            conversation={
                "messages": [{
                    "id": "m1",
                    "direction": "inbound",
                    "body": latest_text,
                }],
            },
        )

    def test_explicit_call_request_creates_immediate_reminder(self):
        before = timezone.now()
        actions, _ = build_controlled_actions(
            decision=SimpleNamespace(qualification_updates=[], crm_actions=[]),
            context=self._context(
                stage_name="Follow-up",
                latest_text="I want to connect, please call me",
            ),
            runtime_policy={"qualification": {"criteria": []}},
            qualification_state={
                "engagement_mode": MODE_CONVERSATION,
                "requirement_states": {},
            },
            requirements=[],
        )
        reminder = next(item for item in actions if item.get("type") == "create_reminder")
        due_at = timezone.datetime.fromisoformat(reminder["due_at"])
        self.assertGreaterEqual(due_at, before)
        self.assertLessEqual(due_at, timezone.now() + timezone.timedelta(minutes=1))

    def test_other_stage_stays_conversation_only_but_can_shift_by_stage_rule(self):
        destination = {
            "id": "human-stage",
            "name": "Human Intervention",
            "description": "Move here when the lead explicitly asks to talk to a person.",
            "pipeline_id": "other-pipeline",
            "pipeline_name": "Escalations",
        }
        actions, _ = build_controlled_actions(
            decision=SimpleNamespace(
                qualification_updates=[{
                    "requirement_id": "budget",
                    "value": "50k",
                    "source_message_id": "m1",
                    "evidence": "50k",
                }],
                crm_actions=[{
                    "type": "pipeline_transition",
                    "stage_shift": {"stage_id": "human-stage"},
                }],
            ),
            context=self._context(
                stage_name="Follow-up",
                latest_text="I want to talk to a person",
                available_stages=[destination],
            ),
            runtime_policy={
                "qualification": {
                    "criteria": [{"id": "budget", "required": True}],
                },
            },
            qualification_state={
                "engagement_mode": MODE_CONVERSATION,
                "requirement_states": {
                    "budget": {"status": "unknown", "value": None},
                },
            },
            requirements=[{"id": "budget", "required": True}],
        )

        self.assertTrue(any(
            item.get("type") == "pipeline_transition"
            and item.get("stage_shift", {}).get("stage_id") == "human-stage"
            for item in actions
        ))
        self.assertFalse(any(
            item.get("type") == "attribute_updates"
            for item in actions
        ))
