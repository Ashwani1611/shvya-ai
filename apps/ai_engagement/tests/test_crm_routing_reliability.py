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
from apps.crm.models import AttributeDefinition, Lead, Pipeline, Stage
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
        self.sales.stages.filter(name="Qualified").update(is_active=False)

        self.qualified_pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Qualified Opportunities",
            is_active=True,
        )
        self.qualified = self.qualified_pipeline.stages.get(name="Qualified")
        # Cross-pipeline fallback must never choose arbitrarily. Make this test
        # organization intentionally have one unambiguous active external target.
        Stage.objects.filter(
            pipeline__organization=self.organization,
            name__iexact="Qualified",
        ).exclude(pk=self.qualified.pk).update(is_active=False)

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

    def _requirements(self):
        return compile_qualification_requirements(
            self.org_info.qualification_requirements
        )["requirements"]

    def _context_and_policy(self, *, message_id, body, requirements):
        self.lead.refresh_from_db()
        context = AIContextBuilder().build(
            organization=self.organization,
            lead=self.lead,
            message_limit=12,
            knowledge_limit=3,
            note_limit=5,
        )
        context.conversation["messages"] = [{
            "id": message_id,
            "direction": "inbound",
            "body": body,
        }]
        profile = compile_org_ai_profile_from_context(context.organization)
        runtime_policy = get_runtime_policy(
            organization=self.organization,
            profile=profile,
        )
        return context, runtime_policy

    def test_backend_captured_answer_uses_description_and_moves_cross_pipeline_qualified(self):
        requirements = self._requirements()
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
        context, runtime_policy = self._context_and_policy(
            message_id="final-answer",
            body="B",
            requirements=requirements,
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

    def test_model_captured_natural_answer_projects_before_attribute_and_qualified_actions(self):
        requirements = self._requirements()
        requirement = requirements[0]
        record_last_asked_requirement(
            self.lead,
            requirement["id"],
            requirements=requirements,
        )
        qualification_state = state_for_lead(self.lead, requirements=requirements)
        context, runtime_policy = self._context_and_policy(
            message_id="natural-answer",
            body="I currently manage them in Excel/Sheets",
            requirements=requirements,
        )
        decision = SimpleNamespace(
            qualification_updates=[{
                "requirement_id": requirement["id"],
                "value": "Excel/Sheets",
                "source_message_id": "natural-answer",
                "evidence": "Excel/Sheets",
            }],
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
            [{"key": "lead_system_answer", "value": "Excel/Sheets"}],
        )
        transition = next(
            item for item in actions if item.get("type") == "pipeline_transition"
        )
        self.assertEqual(transition["stage_shift"]["stage_id"], str(self.qualified.id))
        projected = result["projected_qualification_state"]
        self.assertTrue(projected["all_requirements_answered"])


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
