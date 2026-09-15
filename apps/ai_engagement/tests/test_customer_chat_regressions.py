from __future__ import annotations

import json
from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.ai_engagement.services import conversation_priority_runtime, qualification_state
from apps.ai_engagement.services.customer_chat_regressions import (
    sanitize_customer_message,
)
from apps.ai_engagement.services.engagement import EngagementService
from apps.ai_engagement.services.organization_profile import (
    compile_qualification_requirements,
)
from apps.ai_engagement.tests.test_precise_orchestration import (
    PreciseEngagementTests,
)


class CustomerChatRegressionTests(SimpleTestCase):
    def test_authored_q_labels_are_not_customer_facing_questions(self):
        raw = (
            "Q1. What is your biggest challenge? A. Slow replies B. Missed follow-ups\n"
            "Q2. Do you currently run ads? A. Yes B. No"
        )
        requirements = compile_qualification_requirements(raw)["requirements"]

        self.assertEqual(len(requirements), 2)
        self.assertTrue(requirements[0]["question"].startswith("What is your biggest challenge?"))
        self.assertTrue(requirements[1]["question"].startswith("Do you currently run ads?"))
        self.assertNotIn("Q1.", requirements[0]["question"])
        self.assertNotIn("Q2.", requirements[1]["question"])

    def test_short_ye_maps_to_yes_only_for_binary_active_question(self):
        result = qualification_state._classify_direct_reply(
            text="Ye",
            question="Do you currently run ads?\nA. Yes\nB. No",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result[0], qualification_state.REQUIREMENT_ANSWERED)
        self.assertEqual(result[1], "Yes")

    def test_functionality_noun_phrase_is_treated_as_customer_information_intent(self):
        self.assertEqual(
            conversation_priority_runtime._intent_kind("Functionality of shvya"),
            "question",
        )

    def test_functionality_noun_phrase_triggers_knowledge_retrieval(self):
        context = SimpleNamespace(
            conversation={
                "messages": [
                    {
                        "id": "m1",
                        "direction": "inbound",
                        "body": "Functionality of shvya",
                    }
                ]
            }
        )

        self.assertTrue(EngagementService()._should_retrieve_knowledge(context=context))

    def test_prompt_payload_sanitizes_q_labels_from_persisted_snapshot(self):
        context = PreciseEngagementTests()._context("Functionality of shvya")
        context.conversation["messages"][0]["id"] = "m1"
        requirement = {
            "id": "challenge",
            "stable_id": "qualification_1",
            "priority": 1,
            "required": True,
            "question": "Q1. What is your biggest challenge?\nA. Slow replies\nB. Missed follow-ups",
            "label": "Q1. What is your biggest challenge?",
            "options": [
                {"key": "A", "value": "Slow replies"},
                {"key": "B", "value": "Missed follow-ups"},
            ],
        }
        state = {
            "qualification_status": "in_progress",
            "qualification_result": "",
            "qualification_completed": False,
            "engagement_mode": "qualification",
            "flow_snapshot": [requirement],
            "requirement_states": {
                "challenge": {
                    "status": "asked",
                    "value": None,
                    "source_message_id": None,
                }
            },
            "current_requirement_id": "challenge",
            "next_requirement_id": "challenge",
            "last_asked_requirement_id": "challenge",
            "answered_requirement_ids": [],
            "qualification_answers": {},
            "processed_message_ids": [],
        }

        raw = EngagementService()._build_input(
            context=context,
            qualification_state=state,
            next_item=requirement,
        )
        payload = json.loads(raw)
        question = payload["qualification_turn"]["current_requirement"]["question"]

        self.assertTrue(question.startswith("What is your biggest challenge?"))
        self.assertNotIn("Q1.", question)

    def test_customer_formatting_leakage_is_removed(self):
        self.assertEqual(
            sanitize_customer_message(
                '"Thanks for sharing the details. Our team will connect with you shortly."'
            ),
            "Thanks for sharing the details. Our team will connect with you shortly.",
        )
        self.assertEqual(
            sanitize_customer_message(
                "Got it. Q2. Where do you currently manage your leads?",
                qualification_context=True,
            ),
            "Got it. Where do you currently manage your leads?",
        )
