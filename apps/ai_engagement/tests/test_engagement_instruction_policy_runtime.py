from __future__ import annotations

from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.ai_engagement.graph.policy_actions import build_controlled_actions
from apps.ai_engagement.graph.runtime_policy import get_runtime_policy
from apps.ai_engagement.models import OrgInfo
from apps.ai_engagement.services.context import AIContextBuilder
from apps.ai_engagement.services.crm_executor import CRMActionExecutor
from apps.ai_engagement.services.engagement_instruction_policy import (
    effective_qualification_source,
    parse_engagement_instruction_sections,
)
from apps.ai_engagement.services.organization_profile import compile_org_ai_profile_from_context
from apps.ai_engagement.services.qualification_state import (
    apply_unambiguous_reply,
    record_last_asked_requirement,
    state_for_lead,
)
from apps.ai_engagement.services.reminder_time_runtime import parse_grounded_due_at
from apps.crm.models import AttributeDefinition, Lead, Pipeline, Stage
from apps.organizations.models import Organization


AUTHORED_POLICY = """
##Qualification criteria
What is your biggest challenge?
A. Slow replies
B. Missed follow-ups
C. Leads going cold
D. No proper tracking
Where do you manage leads?
A. WhatsApp
B. Excel/Sheets
C. CRM
D. Multiple places
How many leads do you receive per day?
A. 0-10
B. 10-30
C. 30+
Do you run ads?
A. Yes
B. No
If not ads, what is your lead source?
A. Referrals
B. Organic search
C. Walk-ins
D. Other
After all required qualification questions are answered, mark the lead qualified.

##Attribute mapped
Q1 -> Biggest Challenge
Q2 -> Lead Management
Q3 -> Daily Leads
Q4 -> Runs Ads
Q5 -> Lead Source

##Stage shifting
If the lead asks to talk to a human, move to Human Intervention.
If the lead says they are a seller, move to Seller in Seller Pipeline.

##Reminders
When the lead provides a concrete date and time to connect, create a reminder.
""".strip()


class EngagementInstructionPolicyParsingTests(SimpleTestCase):
    def test_hash_headings_are_compiled_as_distinct_policy_sections(self):
        sections = parse_engagement_instruction_sections(AUTHORED_POLICY)
        self.assertIn("What is your biggest challenge?", sections["qualification_criteria"])
        self.assertIn("Q3 -> Daily Leads", sections["attribute_mapped"])
        self.assertIn("Human Intervention", sections["stage_shifting"])
        self.assertIn("concrete date and time", sections["reminders"])

    def test_qualification_section_is_fallback_questionnaire_when_dedicated_field_empty(self):
        source, source_name = effective_qualification_source(
            qualification_requirements="",
            engagement_instructions=AUTHORED_POLICY,
        )
        self.assertIn("What is your biggest challenge?", source)
        self.assertNotIn("mark the lead qualified", source)
        self.assertEqual(
            source_name,
            "engagement_instructions.qualification_criteria",
        )

    def test_dedicated_qualification_field_keeps_precedence(self):
        source, source_name = effective_qualification_source(
            qualification_requirements="What is your budget?",
            engagement_instructions=AUTHORED_POLICY,
        )
        self.assertEqual(source, "What is your budget?")
        self.assertEqual(source_name, "qualification_requirements")

    def test_grounded_reminder_parser_accepts_common_date_time_language(self):
        for text in (
            "Tomorrow at 5 PM",
            "Next Monday at 4:30 pm",
            "13 Sep 2030 at 17:30",
            "in 2 hours",
        ):
            with self.subTest(text=text):
                due_at = parse_grounded_due_at(text)
                self.assertIsNotNone(due_at)
                parsed = timezone.datetime.fromisoformat(due_at)
                self.assertGreater(parsed, timezone.now())


class AuthoredPolicyCRMIntegrationTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Authored AI Brain Org")
        self.sales = Pipeline.objects.create(
            organization=self.organization,
            name="Sales",
            is_active=True,
        )
        self.new_lead = self.sales.stages.get(name="New leads")
        self.qualified = self.sales.stages.get(name="Qualified")
        self.follow_up = Stage.objects.create(
            pipeline=self.sales,
            name="Follow-up",
            description="Normal conversation stage.",
            is_active=True,
            display_order=60,
        )

        self.seller_pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Seller Pipeline",
            is_active=True,
        )
        self.seller = Stage.objects.create(
            pipeline=self.seller_pipeline,
            name="Seller",
            description="Move here when the lead says they are a seller.",
            is_active=True,
            display_order=80,
        )
        self.human = Stage.objects.create(
            pipeline=self.sales,
            name="Human Intervention",
            description="Move here when the lead asks to talk to a human.",
            is_active=True,
            display_order=70,
        )

        self.org_info = OrgInfo.objects.create(
            organization=self.organization,
            qualification_requirements="",
            engagement_instructions=AUTHORED_POLICY,
            bot_languages="English",
            ai_enabled=True,
        )

        definitions = (
            ("Biggest Challenge", "biggest_challenge"),
            ("Lead Management", "lead_management"),
            ("Daily Leads", "daily_leads"),
            ("Runs Ads", "runs_ads"),
            ("Lead Source", "lead_source_answer"),
        )
        for name, key in definitions:
            AttributeDefinition.objects.create(
                organization=self.organization,
                name=name,
                key=key,
                field_type=AttributeDefinition.FieldType.TEXT,
            )

        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.sales,
            stage=self.new_lead,
            name="Authored Policy Lead",
            phone="+919000009991",
        )

    def _context_policy_requirements(self, *, message_id: str, body: str):
        self.lead.refresh_from_db()
        context = AIContextBuilder().build(
            organization=self.organization,
            lead=self.lead,
            message_limit=20,
            knowledge_limit=3,
            note_limit=5,
        )
        context.conversation["messages"] = [{
            "id": message_id,
            "direction": "inbound",
            "body": body,
        }]
        profile = compile_org_ai_profile_from_context(context.organization)
        requirements = profile["qualification"]["requirements"]
        runtime_policy = get_runtime_policy(
            organization=self.organization,
            profile=profile,
        )
        return context, runtime_policy, requirements, profile

    def test_runtime_profile_and_policy_use_authored_headings(self):
        _context, policy, requirements, profile = self._context_policy_requirements(
            message_id="m0",
            body="Hi",
        )
        self.assertEqual(len(requirements), 5)
        self.assertEqual(
            profile["qualification"]["source"],
            "engagement_instructions.qualification_criteria",
        )
        self.assertEqual(
            policy["qualification"]["source"],
            profile["qualification"]["source"],
        )
        self.assertIn("Q3 -> Daily Leads", policy["crm"]["attribute_mapped"])
        self.assertTrue(any("Seller" in rule for rule in policy["crm"]["stage_shifting"]))

    def test_five_answers_fill_explicit_mappings_without_magic_completion_side_effects(self):
        _context, _policy, requirements, _profile = self._context_policy_requirements(
            message_id="start",
            body="Hi",
        )
        self.assertEqual(len(requirements), 5)
        answers = ["A", "B", "C", "A", "A"]
        expected = {
            "biggest_challenge": "Slow replies",
            "lead_management": "Excel/Sheets",
            "daily_leads": "30+",
            "runs_ads": "Yes",
            "lead_source_answer": "Referrals",
        }

        for index, (requirement, answer) in enumerate(zip(requirements, answers), start=1):
            message_id = f"answer-{index}"
            record_last_asked_requirement(
                self.lead,
                requirement["id"],
                requirements=requirements,
            )
            captured = apply_unambiguous_reply(
                lead=self.lead,
                requirements=requirements,
                text=answer,
                source_message_id=message_id,
            )
            self.assertTrue(captured["changed"])

            context, runtime_policy, pinned_requirements, _profile = self._context_policy_requirements(
                message_id=message_id,
                body=answer,
            )
            qualification_state = state_for_lead(
                self.lead,
                requirements=pinned_requirements,
            )
            actions, _result = build_controlled_actions(
                decision=SimpleNamespace(qualification_updates=[], crm_actions=[]),
                context=context,
                runtime_policy=runtime_policy,
                qualification_state=qualification_state,
                requirements=pinned_requirements,
            )

            CRMActionExecutor().execute(
                organization=self.organization,
                lead=self.lead,
                actions=actions,
            )
            self.lead.refresh_from_db()
            # Explicit mapping may be projected by authored policy, but completion
            # stage and summary-note side effects belong to the execution contract.
            self.assertEqual(self.lead.stage_id, self.new_lead.id)

        self.lead.refresh_from_db()
        for key, value in expected.items():
            self.assertEqual(self.lead.attributes.get(key), value)

    def test_non_new_stage_can_shift_cross_pipeline_from_authored_stage_rule(self):
        self.lead.stage = self.follow_up
        self.lead.save(update_fields=["stage", "updated_at"])

        context, runtime_policy, requirements, _profile = self._context_policy_requirements(
            message_id="seller-turn",
            body="I am a seller and want to sell my property",
        )
        qualification_state = state_for_lead(self.lead, requirements=requirements)
        actions, _result = build_controlled_actions(
            decision=SimpleNamespace(
                qualification_updates=[],
                crm_actions=[{
                    "type": "pipeline_transition",
                    "stage_shift": {"stage_id": str(self.seller.id)},
                }],
            ),
            context=context,
            runtime_policy=runtime_policy,
            qualification_state=qualification_state,
            requirements=requirements,
        )

        self.assertFalse(any(item.get("type") == "attribute_updates" for item in actions))
        transition = next(item for item in actions if item.get("type") == "pipeline_transition")
        self.assertEqual(transition["stage_shift"]["stage_id"], str(self.seller.id))

        CRMActionExecutor().execute(
            organization=self.organization,
            lead=self.lead,
            actions=actions,
        )
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.pipeline_id, self.seller_pipeline.id)
        self.assertEqual(self.lead.stage_id, self.seller.id)

    def test_stage_description_can_route_without_model_stage_proposal(self):
        self.lead.stage = self.follow_up
        self.lead.save(update_fields=["stage", "updated_at"])

        context, runtime_policy, requirements, _profile = self._context_policy_requirements(
            message_id="human-turn",
            body="I want to talk to a human please",
        )
        qualification_state = state_for_lead(self.lead, requirements=requirements)
        actions, _result = build_controlled_actions(
            decision=SimpleNamespace(qualification_updates=[], crm_actions=[]),
            context=context,
            runtime_policy=runtime_policy,
            qualification_state=qualification_state,
            requirements=requirements,
        )
        transition = next(item for item in actions if item.get("type") == "pipeline_transition")
        self.assertEqual(transition["stage_shift"]["stage_id"], str(self.human.id))

    def test_concrete_date_time_creates_reminder_in_conversation_stage(self):
        self.lead.stage = self.follow_up
        self.lead.save(update_fields=["stage", "updated_at"])
        context, runtime_policy, requirements, _profile = self._context_policy_requirements(
            message_id="reminder-turn",
            body="Please connect with me next Monday at 5 PM",
        )
        actions, _result = build_controlled_actions(
            decision=SimpleNamespace(qualification_updates=[], crm_actions=[]),
            context=context,
            runtime_policy=runtime_policy,
            qualification_state=state_for_lead(self.lead, requirements=requirements),
            requirements=requirements,
        )
        reminder = next(item for item in actions if item.get("type") == "create_reminder")
        self.assertGreater(
            timezone.datetime.fromisoformat(reminder["due_at"]),
            timezone.now(),
        )
