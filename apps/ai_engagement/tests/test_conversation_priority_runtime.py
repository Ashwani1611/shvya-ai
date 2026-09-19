from __future__ import annotations
from tests.playbook_fixtures import replace_playbook_questions, qualification_questions


import json
from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.ai_engagement.services.engagement import (
    EngagementDecision,
    EngagementError,
    EngagementService,
)
from apps.ai_engagement.services.organization_profile import (
    compile_qualification_requirements,
)
from apps.ai_engagement.tests.test_precise_orchestration import (
    PreciseEngagementTests,
)


class ConversationPriorityPayloadTests(SimpleTestCase):
    def test_full_questionnaire_is_not_exposed_to_generation_payload(self):
        context = PreciseEngagementTests()._context("I need some details")
        context.conversation["messages"][0]["id"] = "inbound-1"
        context.organization["ai_playbook"] = replace_playbook_questions(context.organization["ai_playbook"], "What is your budget?\n"
            "Which city are you in?\n"
            "Which product do you need?")
        requirements = compile_qualification_requirements(
            qualification_questions(context.organization["ai_playbook"])
        )["requirements"]
        states = {
            item["id"]: {
                "status": "unknown",
                "value": None,
                "source_message_id": None,
            }
            for item in requirements
        }
        qualification_state = {
            "qualification_status": "in_progress",
            "qualification_result": "",
            "qualification_completed": False,
            "engagement_mode": "qualification",
            "flow_snapshot": requirements,
            "requirement_states": states,
            "current_requirement_id": requirements[0]["id"],
            "next_requirement_id": requirements[0]["id"],
            "last_asked_requirement_id": requirements[0]["id"],
            "answered_requirement_ids": [],
            "qualification_answers": {},
            "processed_message_ids": [],
        }

        raw = EngagementService()._build_input(
            context=context,
            qualification_state=qualification_state,
            next_item=requirements[0],
        )
        payload = json.loads(raw)

        lead_qualification = payload["lead"]["qualification"]
        self.assertNotIn("flow_snapshot", lead_qualification)
        self.assertNotIn("requirement_states", lead_qualification)
        self.assertNotIn("history", lead_qualification)

        turn = payload["qualification_turn"]
        self.assertEqual(turn["current_requirement"]["id"], requirements[0]["id"])
        self.assertEqual(
            turn["next_requirement_if_current_answered"]["id"],
            requirements[1]["id"],
        )
        self.assertEqual(
            [item["id"] for item in turn["capture_only_requirements"]],
            [requirements[2]["id"]],
        )
        self.assertFalse(turn["capture_only_requirements"][0]["askable"])
        self.assertNotIn(requirements[2]["question"], raw)


class ConversationPriorityValidationTests(SimpleTestCase):
    def setUp(self):
        self.service = EngagementService()
        self.requirements = [
            {
                "id": "city",
                "stable_id": "city",
                "priority": 1,
                "required": True,
                "question": "Which city are you in?",
                "options": [],
            },
            {
                "id": "budget",
                "stable_id": "budget",
                "priority": 2,
                "required": True,
                "question": "What is your budget?",
                "options": [],
            },
            {
                "id": "product",
                "stable_id": "product",
                "priority": 3,
                "required": True,
                "question": "Which product do you need?",
                "options": [],
            },
        ]

    def _context(self, body: str):
        return SimpleNamespace(
            conversation={
                "messages": [
                    {
                        "id": "m1",
                        "direction": "inbound",
                        "body": body,
                    }
                ]
            },
            lead={"attributes": {}},
            stage={"name": "New leads"},
            pipeline={},
            organization={},
        )

    def _state(self, *, current="city", last_asked=None):
        return {
            "qualification_status": "in_progress",
            "qualification_result": "",
            "qualification_completed": False,
            "engagement_mode": "qualification",
            "current_requirement_id": current,
            "next_requirement_id": current,
            "last_asked_requirement_id": last_asked,
            "requirement_states": {
                item["id"]: {
                    "status": "asked" if item["id"] == last_asked else "unknown",
                    "value": None,
                    "source_message_id": None,
                }
                for item in self.requirements
            },
            "processed_message_ids": [],
        }

    def _decision(
        self,
        *,
        message,
        next_requirement_id,
        reason_code,
        qualification_updates=None,
    ):
        return EngagementDecision(
            should_engage=True,
            message=message,
            file_document_id=None,
            crm_actions=[],
            reason=reason_code,
            reason_code=reason_code,
            next_requirement_id=next_requirement_id,
            qualification_updates=qualification_updates or [],
            model="test",
        )

    def test_direct_customer_question_cannot_be_replaced_by_qualification(self):
        context = self._context("What are your prices?")
        decision = self._decision(
            message="Which city are you in?",
            next_requirement_id="city",
            reason_code="QUALIFICATION_NEXT",
        )
        with self.assertRaisesRegex(EngagementError, "immediate question/request/problem"):
            self.service._validate_qualification_decision(
                decision=decision,
                context=context,
                requirements=self.requirements,
                qualification_state=self._state(),
            )

    def test_direct_customer_question_may_be_answered_then_qualification_continues(self):
        context = self._context("What are your prices?")
        decision = self._decision(
            message=(
                "Pricing depends on the selected plan and verified organization details. "
                "Which city are you in?"
            ),
            next_requirement_id="city",
            reason_code="ANSWER_ORG_QUESTION",
        )
        self.service._validate_qualification_decision(
            decision=decision,
            context=context,
            requirements=self.requirements,
            qualification_state=self._state(),
        )

    def test_customer_reply_cannot_dump_future_qualification_questions(self):
        context = self._context("Hello")
        decision = self._decision(
            message="Which city are you in? What is your budget?",
            next_requirement_id="city",
            reason_code="NORMAL_CONVERSATION",
        )
        with self.assertRaisesRegex(EngagementError, "not the backend-authorized"):
            self.service._validate_qualification_decision(
                decision=decision,
                context=context,
                requirements=self.requirements,
                qualification_state=self._state(),
            )

    def test_future_requirement_information_can_be_saved_without_asking_it(self):
        requirements = [
            self.requirements[1],
            self.requirements[0],
            self.requirements[2],
        ]
        state = {
            "qualification_status": "in_progress",
            "qualification_result": "",
            "qualification_completed": False,
            "engagement_mode": "qualification",
            "current_requirement_id": "budget",
            "next_requirement_id": "budget",
            "last_asked_requirement_id": "budget",
            "requirement_states": {
                "budget": {"status": "asked", "value": None, "source_message_id": None},
                "city": {"status": "unknown", "value": None, "source_message_id": None},
                "product": {"status": "unknown", "value": None, "source_message_id": None},
            },
            "processed_message_ids": [],
        }
        context = self._context("My budget is 50k and I am in Delhi")
        decision = self._decision(
            message="Thanks. Which product do you need?",
            next_requirement_id="product",
            reason_code="QUALIFICATION_NEXT",
            qualification_updates=[
                {
                    "requirement_id": "budget",
                    "value": "50k",
                    "source_message_id": "m1",
                    "evidence": "50k",
                },
                {
                    "requirement_id": "city",
                    "value": "Delhi",
                    "source_message_id": "m1",
                    "evidence": "Delhi",
                },
            ],
        )
        self.service._validate_qualification_decision(
            decision=decision,
            context=context,
            requirements=requirements,
            qualification_state=state,
        )
