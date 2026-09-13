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
    apply_unambiguous_reply,
    record_last_asked_requirement,
    state_for_lead,
)
from apps.crm.models import AttributeDefinition, Lead, Pipeline
from apps.organizations.models import Organization


class QualificationRoutingReliabilityTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Routing Reliability Org")
        self.sales = Pipeline.objects.create(
            organization=self.organization,
            name="Inbound Sales",
            is_active=True,
        )
        self.new_lead = self.sales.stages.get(name="New leads")
        # Reproduce organizations where Qualified lives in another pipeline.
        self.sales.stages.filter(name="Qualified").update(is_active=False)

        self.qualified_pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Qualified Opportunities",
            is_active=True,
        )
        self.qualified = self.qualified_pipeline.stages.get(name="Qualified")

        self.org_info = OrgInfo.objects.create(
            organization=self.organization,
            qualification_requirements=(
                "Where do you currently manage leads?\n"
                "A. WhatsApp\n"
                "B. Excel/Sheets\n"
                "C. CRM\n"
                "D. Multiple places"
            ),
            bot_languages="English",
            engagement_instructions="Reply naturally and concisely.",
            ai_enabled=True,
        )
        AttributeDefinition.objects.create(
            organization=self.organization,
            name="Lead System Answer",
            key="lead_system_answer",
            description=(
                "Where the lead currently manages leads, for example WhatsApp, "
                "Excel/Sheets, CRM, or multiple places. Fill this from that qualification answer."
            ),
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.sales,
            stage=self.new_lead,
            name="Routing Lead",
            phone="+919000000777",
        )

    def test_description_mapped_answer_fills_attribute_and_moves_cross_pipeline_qualified(self):
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
            source_message_id="final-answer",
        )
        self.assertTrue(direct["changed"])
        self.assertEqual(direct["state"]["qualification_status"], "completed")

        self.lead.refresh_from_db()
        qualification_state = state_for_lead(self.lead, requirements=requirements)
        self.assertEqual(qualification_state["qualified_stage_id"], str(self.qualified.id))

        context = AIContextBuilder().build(
            organization=self.organization,
            lead=self.lead,
            message_limit=12,
            knowledge_limit=3,
            note_limit=5,
        )
        context.conversation["messages"] = [{
            "id": "final-answer",
            "direction": "inbound",
            "body": "B",
        }]
        profile = compile_org_ai_profile_from_context(context.organization)
        runtime_policy = get_runtime_policy(
            organization=self.organization,
            profile=profile,
        )

        actions, _ = build_controlled_actions(
            decision=SimpleNamespace(qualification_updates=[], crm_actions=[]),
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
            [{"key": "lead_system_answer", "value": "Excel/Sheets"}],
        )
        transition = next(
            item for item in actions if item.get("type") == "pipeline_transition"
        )
        self.assertEqual(transition["stage_shift"]["stage_id"], str(self.qualified.id))

        CRMActionExecutor().execute(
            organization=self.organization,
            lead=self.lead,
            actions=actions,
        )
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.attributes["lead_system_answer"], "Excel/Sheets")
        self.assertEqual(self.lead.pipeline_id, self.qualified_pipeline.id)
        self.assertEqual(self.lead.stage_id, self.qualified.id)


class ConversationRoutingReliabilityTests(SimpleTestCase):
    def _context(self, *, body, stage_name="Follow-up", available_stages=None):
        return SimpleNamespace(
            stage={"id": "current", "name": stage_name},
            pipeline={
                "attribute_definitions": [],
                "available_stages": available_stages or [],
            },
            conversation={
                "messages": [{
                    "id": "m1",
                    "direction": "inbound",
                    "body": body,
                }],
            },
        )

    def test_date_and_time_only_creates_reminder(self):
        actions, _ = build_controlled_actions(
            decision=SimpleNamespace(qualification_updates=[], crm_actions=[]),
            context=self._context(body="Tomorrow 5 PM works for me"),
            runtime_policy={"qualification": {"criteria": []}},
            qualification_state={"engagement_mode": "conversation", "requirement_states": {}},
            requirements=[],
        )
        reminder = next(item for item in actions if item.get("type") == "create_reminder")
        due_at = timezone.datetime.fromisoformat(reminder["due_at"])
        self.assertGreater(due_at, timezone.now())
        self.assertEqual(timezone.localtime(due_at).hour, 17)

    def test_non_new_stage_can_execute_explicit_cross_pipeline_stage_action(self):
        destination = {
            "id": "seller-stage",
            "name": "Seller",
            "description": "",
            "pipeline_id": "seller-pipeline",
            "pipeline_name": "Seller Pipeline",
        }
        actions, _ = build_controlled_actions(
            decision=SimpleNamespace(
                qualification_updates=[],
                crm_actions=[{
                    "type": "pipeline_transition",
                    "stage_shift": {"stage_id": "seller-stage"},
                }],
            ),
            context=self._context(
                body="Please move this to Seller",
                available_stages=[destination],
            ),
            runtime_policy={"qualification": {"criteria": []}},
            qualification_state={"engagement_mode": "conversation", "requirement_states": {}},
            requirements=[],
        )
        self.assertTrue(any(
            item.get("type") == "pipeline_transition"
            and item.get("stage_shift", {}).get("stage_id") == "seller-stage"
            for item in actions
        ))
