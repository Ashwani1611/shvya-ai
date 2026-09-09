from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase, override_settings

from apps.ai_engagement.models import OrgInfo
from apps.ai_engagement.services.ai_provider import OpenAIProvider
from apps.ai_engagement.services.engagement import EngagementService
from apps.ai_engagement.services.organization_profile import (
    compile_org_ai_profile_from_context,
    compile_qualification_requirements,
)
from apps.ai_engagement.services.qualification_state import (
    QUALIFICATION_STATE_KEY,
    REQUIREMENT_ANSWERED,
    REQUIREMENT_UNCLEAR,
    _classify_direct_reply,
    apply_unambiguous_reply,
    next_requirement,
    record_last_asked_requirement,
    state_for_lead,
)
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization


class OrganizationAIProfileTests(SimpleTestCase):
    def test_explicit_questions_compile_in_priority_order(self):
        result = compile_qualification_requirements(
            "What is your budget?\n"
            "Which city are you looking in?\n"
            "When do you plan to buy?"
        )
        requirements = result["requirements"]
        self.assertEqual(len(requirements), 3)
        self.assertEqual([item["priority"] for item in requirements], [1, 2, 3])
        self.assertTrue(all(item["can_direct_ask"] for item in requirements))
        self.assertEqual(len({item["id"] for item in requirements}), 3)

    def test_compact_legacy_requirement_list_is_normalized(self):
        result = compile_qualification_requirements(
            "Identify course interest, budget, timeline, and buying intent."
        )
        labels = [item["label"].casefold() for item in result["requirements"]]
        self.assertEqual(labels, [
            "course interest",
            "budget",
            "timeline",
            "buying intent",
        ])

    def test_runtime_profiles_do_not_leak_between_organizations(self):
        first = compile_org_ai_profile_from_context({
            "name": "Org One",
            "about": "Only sells solar panels.",
            "bot_languages": "English",
            "qualification_requirements": "What is your budget?",
            "engagement_instructions": "Be formal.",
        })
        second = compile_org_ai_profile_from_context({
            "name": "Org Two",
            "about": "Only provides fitness coaching.",
            "bot_languages": "Hindi",
            "qualification_requirements": "When do you want to start?",
            "engagement_instructions": "Be conversational.",
        })
        self.assertEqual(first["identity"]["name"], "Org One")
        self.assertEqual(second["identity"]["name"], "Org Two")
        self.assertNotEqual(first["identity"]["about"], second["identity"]["about"])
        self.assertNotEqual(
            first["qualification"]["requirements"][0]["id"],
            second["qualification"]["requirements"][0]["id"],
        )


class DirectQualificationReplyTests(SimpleTestCase):
    def test_budget_numeric_value_can_use_zero_llm_path(self):
        result = _classify_direct_reply(
            text="2 crore",
            question="What is your budget?",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result[0], REQUIREMENT_ANSWERED)

    def test_city_is_not_misbound_to_budget(self):
        self.assertIsNone(
            _classify_direct_reply(
                text="Gurgaon",
                question="What is your budget?",
            )
        )

    def test_boolean_no_only_binds_to_boolean_question(self):
        result = _classify_direct_reply(
            text="No",
            question="Have you purchased property before?",
        )
        self.assertEqual(result[0], REQUIREMENT_ANSWERED)
        self.assertFalse(result[1])
        self.assertIsNone(
            _classify_direct_reply(
                text="No",
                question="What is your budget?",
            )
        )

    def test_uncertain_short_reply_is_structured_as_unclear(self):
        result = _classify_direct_reply(
            text="Not sure",
            question="When are you planning to buy?",
        )
        self.assertEqual(result[0], REQUIREMENT_UNCLEAR)


class QualificationStatePersistenceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(name="Precision State Org")
        cls.pipeline = Pipeline.objects.create(
            organization=cls.organization,
            name="Precision Pipeline",
            is_active=True,
        )
        cls.stage = Stage.objects.create(
            pipeline=cls.pipeline,
            name="New Lead",
            display_order=0,
            is_active=True,
        )

    def setUp(self):
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Precision Lead",
            phone="+919900000001",
            attributes={},
            ai_enabled=True,
        )
        self.requirements = compile_qualification_requirements(
            "What is your budget?\nWhen do you plan to buy?"
        )["requirements"]

    def test_python_selects_next_requirement_deterministically(self):
        state = state_for_lead(self.lead, requirements=self.requirements)
        selected = next_requirement(self.requirements, state["requirement_states"])
        self.assertEqual(selected["id"], self.requirements[0]["id"])

    def test_last_asked_short_answer_persists_and_advances(self):
        first_id = self.requirements[0]["id"]
        record_last_asked_requirement(
            self.lead,
            first_id,
            requirements=self.requirements,
        )
        result = apply_unambiguous_reply(
            lead=self.lead,
            requirements=self.requirements,
            text="2 crore",
            source_message_id="msg-1",
        )
        self.assertTrue(result["changed"])
        self.assertEqual(
            result["state"]["requirement_states"][first_id]["status"],
            REQUIREMENT_ANSWERED,
        )
        self.assertEqual(
            result["state"]["next_requirement_id"],
            self.requirements[1]["id"],
        )
        self.lead.refresh_from_db()
        saved = self.lead.attributes[QUALIFICATION_STATE_KEY]
        self.assertEqual(saved["last_asked_requirement_id"], first_id)


@override_settings(OPENAI_API_KEY="test-key-never-sent")
class StructuredProviderTests(SimpleTestCase):
    def test_responses_api_receives_json_schema_format(self):
        client = Mock()
        client.responses.create.return_value = SimpleNamespace(
            output_text=json.dumps({"ok": True}),
            model="gpt-4.1-nano",
        )
        provider = OpenAIProvider(client=client)
        provider.generate_text(
            instructions="Return the object.",
            input_text="test",
            response_schema={
                "name": "test_schema",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
            },
        )
        kwargs = client.responses.create.call_args.kwargs
        self.assertEqual(kwargs["text"]["format"]["type"], "json_schema")
        self.assertEqual(kwargs["text"]["format"]["name"], "test_schema")
        self.assertTrue(kwargs["text"]["format"]["strict"])


class CompactEngagementContextTests(SimpleTestCase):
    @patch.dict("os.environ", {"AI_ENGAGEMENT_CONTEXT_MAX_CHARS": "2000"})
    def test_recent_context_budget_keeps_newest_messages(self):
        service = EngagementService.__new__(EngagementService)
        messages = [
            {
                "id": str(index),
                "direction": "inbound",
                "body": f"message-{index}-" + ("x" * 700),
            }
            for index in range(1, 7)
        ]
        compact = service._compact_conversation({
            "message_count": len(messages),
            "messages": messages,
        })
        self.assertTrue(compact["truncated"])
        self.assertEqual(compact["messages"][-1]["id"], "6")
        self.assertLess(len(compact["messages"]), len(messages))

    def test_prompt_requires_org_alignment_and_one_next_requirement(self):
        from apps.ai_engagement.prompts.engagement import (
            CUSTOMER_ENGAGEMENT_INSTRUCTIONS,
        )

        self.assertIn("Organization AI Profile", CUSTOMER_ENGAGEMENT_INSTRUCTIONS)
        self.assertIn("ONLY new qualification", CUSTOMER_ENGAGEMENT_INSTRUCTIONS)
        self.assertIn("newest explicit customer", CUSTOMER_ENGAGEMENT_INSTRUCTIONS)
