from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.ai_engagement.graph import evidence as evidence_module
from apps.ai_engagement.graph.evidence import SAFE_UNKNOWN_REPLY
from apps.ai_engagement.graph.policy_actions import build_controlled_actions
from apps.ai_engagement.graph.runtime_policy import get_runtime_policy
from apps.ai_engagement.models import OrgInfo
from apps.ai_engagement.services.ai_provider import AITextResult
from apps.ai_engagement.services.context import AIContextBuilder
from apps.ai_engagement.services.crm_executor import CRMActionExecutor
from apps.ai_engagement.services.engagement import EngagementDecision
from apps.ai_engagement.services.organization_profile import (
    compile_org_ai_profile_from_context,
    compile_qualification_requirements,
)
from apps.ai_engagement.services.qualification_state import (
    REQUIREMENT_ANSWERED,
    apply_unambiguous_reply,
    record_last_asked_requirement,
    state_for_lead,
)
from apps.crm.models import AttributeDefinition, Lead, Pipeline
from apps.organizations.models import Organization


class QualificationAnswerRoutingPriorityTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Qualification Priority Org")
        self.pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Sales",
            is_active=True,
        )
        self.new_lead = self.pipeline.stages.get(name="New leads")
        self.qualified = self.pipeline.stages.get(name="Qualified")
        self.org_info = OrgInfo.objects.create(
            organization=self.organization,
            bot_languages="English",
            engagement_instructions="Reply naturally and concisely.",
            ai_enabled=True,
        )

    def _lead(self, suffix: str) -> Lead:
        return Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.new_lead,
            name=f"Lead {suffix}",
            phone=f"+9190000{int(suffix):05d}" if suffix.isdigit() else "+919111111111",
        )

    def _requirements(self, raw: str):
        self.org_info.qualification_requirements = raw
        self.org_info.save(update_fields=["qualification_requirements", "updated_at"])
        return compile_qualification_requirements(raw)["requirements"]

    def _context_and_policy(self, *, lead: Lead, message_id: str, body: str):
        context = AIContextBuilder().build(
            organization=self.organization,
            lead=lead,
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

    def test_option_aliases_and_authored_text_bind_only_to_active_requirement(self):
        requirements = self._requirements(
            "How many leads do you typically receive per day?\n"
            "A. 0-10\n"
            "B. 10-30\n"
            "C. 30+\n"
            "Do you currently run ads?\n"
            "A. Yes\n"
            "B. No"
        )
        cases = {
            "B": "10-30",
            "b": "10-30",
            "OPTION B": "10-30",
            "Option B": "10-30",
            "B)": "10-30",
            "10-30": "10-30",
            "0–10": "0-10",
            "30+ leads": "30+",
        }
        for index, (body, expected) in enumerate(cases.items(), start=1):
            with self.subTest(body=body):
                lead = self._lead(str(index))
                record_last_asked_requirement(
                    lead,
                    requirements[0]["id"],
                    requirements=requirements,
                )
                result = apply_unambiguous_reply(
                    lead=lead,
                    requirements=requirements,
                    text=body,
                    source_message_id=f"option-{index}",
                )
                self.assertTrue(result["changed"])
                self.assertEqual(result["answer_status"], REQUIREMENT_ANSWERED)
                state = result["state"]
                self.assertEqual(
                    state["requirement_states"][requirements[0]["id"]]["value"],
                    expected,
                )
                self.assertEqual(state["next_requirement_id"], requirements[1]["id"])

    def test_natural_numeric_answer_normalizes_and_persists_described_attribute(self):
        requirements = self._requirements(
            "How many leads do you typically receive per day?\n"
            "A. 0-10\n"
            "B. 10-30\n"
            "C. 30+\n"
            "Do you currently run ads?\n"
            "A. Yes\n"
            "B. No"
        )
        AttributeDefinition.objects.create(
            organization=self.organization,
            name="LEADS/D",
            key="leads_per_day",
            description="Daily number of leads received by the business.",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        lead = self._lead("20")
        record_last_asked_requirement(
            lead,
            requirements[0]["id"],
            requirements=requirements,
        )
        result = apply_unambiguous_reply(
            lead=lead,
            requirements=requirements,
            text="We get around 20 leads daily.",
            source_message_id="natural-20",
        )
        self.assertTrue(result["changed"])
        self.assertEqual(
            result["state"]["requirement_states"][requirements[0]["id"]]["value"],
            "10-30",
        )

        lead.refresh_from_db()
        qualification_state = state_for_lead(lead, requirements=requirements)
        context, runtime_policy = self._context_and_policy(
            lead=lead,
            message_id="natural-20",
            body="We get around 20 leads daily.",
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
        self.assertIn(
            {"key": "leads_per_day", "value": "10-30"},
            attribute_action["updates"],
        )

        CRMActionExecutor().execute(
            organization=self.organization,
            lead=lead,
            actions=[attribute_action],
        )
        lead.refresh_from_db()
        self.assertEqual(lead.attributes["leads_per_day"], "10-30")

    def test_natural_yes_and_option_keyword_are_active_question_answers(self):
        yes_requirements = self._requirements(
            "Do you currently run ads?\n"
            "A. Yes\n"
            "B. No\n"
            "Where do your leads mainly come from?\n"
            "A. Referrals\n"
            "B. Organic search"
        )
        lead = self._lead("30")
        record_last_asked_requirement(
            lead,
            yes_requirements[0]["id"],
            requirements=yes_requirements,
        )
        result = apply_unambiguous_reply(
            lead=lead,
            requirements=yes_requirements,
            text="Yes, we run Meta ads.",
            source_message_id="ads-yes",
        )
        self.assertEqual(
            result["state"]["requirement_states"][yes_requirements[0]["id"]]["value"],
            "Yes",
        )

        manage_requirements = self._requirements(
            "Where do you currently manage leads?\n"
            "A. WhatsApp chats\n"
            "B. Excel / Sheets\n"
            "C. CRM\n"
            "D. Multiple places\n"
            "How many leads do you receive per day?\n"
            "A. 0-10\n"
            "B. 10-30\n"
            "C. 30+"
        )
        lead = self._lead("31")
        record_last_asked_requirement(
            lead,
            manage_requirements[0]["id"],
            requirements=manage_requirements,
        )
        result = apply_unambiguous_reply(
            lead=lead,
            requirements=manage_requirements,
            text="We mostly manage leads through WhatsApp.",
            source_message_id="manage-wa",
        )
        self.assertEqual(
            result["state"]["requirement_states"][manage_requirements[0]["id"]]["value"],
            "WhatsApp chats",
        )

    def test_completed_natural_answer_persists_attribute_and_qualified_stage(self):
        requirements = self._requirements(
            "How many leads do you typically receive per day?\n"
            "A. 0-10\n"
            "B. 10-30\n"
            "C. 30+\n"
            "All questions are required"
        )
        AttributeDefinition.objects.create(
            organization=self.organization,
            name="LEADS/D",
            key="leads_per_day",
            description="Daily number of leads received by the business.",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        lead = self._lead("40")
        record_last_asked_requirement(
            lead,
            requirements[0]["id"],
            requirements=requirements,
        )
        result = apply_unambiguous_reply(
            lead=lead,
            requirements=requirements,
            text="About 25 leads a day",
            source_message_id="final-natural",
        )
        self.assertEqual(result["state"]["qualification_status"], "completed")

        lead.refresh_from_db()
        qualification_state = state_for_lead(lead, requirements=requirements)
        context, runtime_policy = self._context_and_policy(
            lead=lead,
            message_id="final-natural",
            body="About 25 leads a day",
        )
        actions, _ = build_controlled_actions(
            decision=SimpleNamespace(qualification_updates=[], crm_actions=[]),
            context=context,
            runtime_policy=runtime_policy,
            qualification_state=qualification_state,
            requirements=requirements,
        )
        CRMActionExecutor().execute(
            organization=self.organization,
            lead=lead,
            actions=actions,
        )
        lead.refresh_from_db()
        self.assertEqual(lead.attributes["leads_per_day"], "10-30")
        self.assertEqual(lead.stage_id, self.qualified.id)
        final_state = state_for_lead(lead, requirements=requirements)
        self.assertEqual(final_state["qualification_status"], "completed")
        self.assertTrue(final_state["qualification_completed"])

    def test_deterministic_qualification_reply_never_reaches_generic_grounding_fallback(self):
        requirements = self._requirements(
            "How many leads do you typically receive per day?\n"
            "A. 0-10\n"
            "B. 10-30\n"
            "C. 30+\n"
            "Do you currently run ads?\n"
            "A. Yes\n"
            "B. No"
        )
        lead = self._lead("50")
        record_last_asked_requirement(
            lead,
            requirements[0]["id"],
            requirements=requirements,
        )
        direct = apply_unambiguous_reply(
            lead=lead,
            requirements=requirements,
            text="B",
            source_message_id="grounding-b",
        )
        next_item = direct["next_requirement"]
        decision = EngagementDecision(
            should_engage=True,
            message=f"Nice. {next_item['question']}",
            file_document_id=None,
            crm_actions=[],
            reason="QUALIFICATION_NEXT",
            reason_code="QUALIFICATION_NEXT",
            next_requirement_id=str(next_item["id"]),
            model="deterministic",
        )
        lead.refresh_from_db()
        context = SimpleNamespace(
            organization={
                "name": self.organization.name,
                "about": "",
                "engagement_instructions": self.org_info.engagement_instructions,
                "bot_languages": self.org_info.bot_languages,
            },
            lead={"attributes": lead.attributes},
            conversation={
                "messages": [{
                    "id": "grounding-b",
                    "direction": "inbound",
                    "body": "B",
                }]
            },
            knowledge=[],
        )
        state = {
            "decision": decision,
            "context": context,
            "organization": self.organization,
            "lead": lead,
            "requirements": requirements,
            "qualification_state": direct["state"],
            "latest_message_id": "grounding-b",
            "latest_text": "B",
            "route": "direct",
            "runtime_policy": {},
        }
        with patch(
            "apps.ai_engagement.graph.evidence.OpenAIProvider.generate_text",
            side_effect=AssertionError("grounding provider must not run"),
        ):
            grounded = evidence_module.check_grounding(state)
        self.assertTrue(grounded["grounding_approved"])
        self.assertTrue(grounded["qualification_answer_authoritative"])
        self.assertNotEqual(decision.message, SAFE_UNKNOWN_REPLY)

    def test_unknown_business_fact_still_uses_hallucination_fallback(self):
        lead = self._lead("60")
        decision = EngagementDecision(
            should_engage=True,
            message="The unavailable plan definitely costs 12345.",
            file_document_id=None,
            crm_actions=[],
            reason="ANSWER_ORG_QUESTION",
            reason_code="ANSWER_ORG_QUESTION",
            model="test",
        )
        context = SimpleNamespace(
            organization={
                "name": self.organization.name,
                "about": "",
                "engagement_instructions": "",
                "bot_languages": "English",
            },
            lead={"attributes": lead.attributes},
            conversation={
                "messages": [{
                    "id": "unknown-fact",
                    "direction": "inbound",
                    "body": "What is the price of the unavailable plan?",
                }]
            },
            knowledge=[],
        )
        state = {
            "decision": decision,
            "context": context,
            "organization": self.organization,
            "lead": lead,
            "requirements": [],
            "qualification_state": {},
            "latest_message_id": "unknown-fact",
            "latest_text": "What is the price of the unavailable plan?",
            "runtime_policy": {},
        }
        # Keep the real constructor: a method-only mock still requires a key.
        # Exercise both provider rejection and fail-closed configuration failure.
        for api_key in ("test-key-never-sent", ""):
            with self.subTest(provider_configured=bool(api_key)), override_settings(
                OPENAI_API_KEY=api_key,
            ), patch(
                "apps.ai_engagement.graph.evidence.OpenAIProvider.generate_text",
                return_value=AITextResult(
                    text='{"approved":false,"reason":"unsupported_fact"}',
                    model="test",
                ),
            ) as grounding_provider:
                grounded = evidence_module.check_grounding(state)
                self.assertFalse(grounded["grounding_approved"])
                self.assertEqual(grounded["decision"].message, SAFE_UNKNOWN_REPLY)
                self.assertFalse(grounded.get("qualification_answer_authoritative"))
                self.assertFalse(grounded.get("grounding_recovered"))
                if api_key:
                    grounding_provider.assert_called_once()
                else:
                    grounding_provider.assert_not_called()
