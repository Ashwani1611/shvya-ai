from __future__ import annotations

from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.ai_engagement.graph.policy_actions import (
    build_controlled_actions,
    evaluate_qualification,
)
from apps.ai_engagement.services.engagement import (
    EngagementError,
    EngagementService,
)
from apps.ai_engagement.services.organization_profile import (
    compile_qualification_requirements,
)
from apps.ai_engagement.services.qualification_state import (
    REQUIREMENT_ANSWERED,
    _classify_direct_reply,
)


class QualificationAuthoringTests(SimpleTestCase):
    def test_multiline_options_compile_as_one_requirement(self):
        compiled = compile_qualification_requirements(
            "Which plan are you interested in?\n"
            "A. Basic\n"
            "B. Pro\n"
            "C. Enterprise\n"
            "Which city are you located in?"
        )

        requirements = compiled["requirements"]
        self.assertEqual(len(requirements), 2)
        self.assertEqual(
            requirements[0]["options"],
            [
                {"key": "A", "value": "Basic"},
                {"key": "B", "value": "Pro"},
                {"key": "C", "value": "Enterprise"},
            ],
        )
        self.assertIn("A. Basic", requirements[0]["question"])
        self.assertEqual(requirements[1]["label"], "Which city are you located in")

    def test_inline_options_compile_as_one_requirement(self):
        compiled = compile_qualification_requirements(
            "Choose a plan: A) Basic B) Pro C) Enterprise"
        )
        requirement = compiled["requirements"][0]
        self.assertEqual(len(requirement["options"]), 3)
        self.assertTrue(requirement["can_direct_ask"])

    def test_numbered_plain_requirements_remain_separate_questions(self):
        compiled = compile_qualification_requirements(
            "1. What is your budget?\n"
            "2. Which city are you located in?"
        )
        self.assertEqual(len(compiled["requirements"]), 2)

    def test_all_required_mode_overrides_optional_marker(self):
        compiled = compile_qualification_requirements(
            "All questions are required\n"
            "What is your budget? optional"
        )
        self.assertEqual(compiled["mode"], "all_required")
        self.assertTrue(compiled["requirements"][0]["required"])


class QualificationAnswerMatchingTests(SimpleTestCase):
    def setUp(self):
        self.question = (
            "Which plan are you interested in?\n"
            "A. Basic\n"
            "B. Pro\n"
            "C. Enterprise"
        )

    def _value(self, text):
        result = _classify_direct_reply(text=text, question=self.question)
        self.assertIsNotNone(result)
        self.assertEqual(result[0], REQUIREMENT_ANSWERED)
        return result[1]

    def test_option_accepts_letter_lowercase_number_and_text(self):
        self.assertEqual(self._value("A"), "Basic")
        self.assertEqual(self._value("a"), "Basic")
        self.assertEqual(self._value("1"), "Basic")
        self.assertEqual(self._value("Basic"), "Basic")
        self.assertEqual(self._value("option 1"), "Basic")
        self.assertEqual(self._value("B"), "Pro")
        self.assertEqual(self._value("2"), "Pro")

    def test_plain_short_freeform_answer_is_bound_to_last_question_type(self):
        result = _classify_direct_reply(
            text="Gurugram",
            question="Which city are you located in?",
        )
        self.assertEqual(result[0], REQUIREMENT_ANSWERED)
        self.assertEqual(result[1], "Gurugram")

    def test_typed_question_does_not_accept_unrelated_freeform_value(self):
        result = _classify_direct_reply(
            text="Gurugram",
            question="What is your budget?",
        )
        self.assertIsNone(result)


class MajorityQualificationTests(SimpleTestCase):
    def _policy(self):
        return {
            "qualification": {
                "mode": "majority",
                "criteria": [
                    {
                        "id": "one",
                        "required": True,
                        "pass_condition": {"operator": "truthy", "value": True},
                    },
                    {
                        "id": "two",
                        "required": True,
                        "pass_condition": {"operator": "truthy", "value": True},
                    },
                    {
                        "id": "three",
                        "required": True,
                        "pass_condition": {"operator": "truthy", "value": True},
                    },
                ],
            }
        }

    def _state(self, one, two, three):
        def item(value):
            if value is None:
                return {"status": "unknown", "value": None}
            return {"status": "answered", "value": value}

        return {
            "requirement_states": {
                "one": item(one),
                "two": item(two),
                "three": item(three),
            }
        }

    def test_majority_mode_qualifies_when_majority_pass_after_all_answers(self):
        result = evaluate_qualification(
            runtime_policy=self._policy(),
            projected_state=self._state(True, True, False),
        )
        self.assertEqual(result["outcome"], "qualified")

    def test_majority_mode_rejects_when_majority_fail(self):
        result = evaluate_qualification(
            runtime_policy=self._policy(),
            projected_state=self._state(True, False, False),
        )
        self.assertEqual(result["outcome"], "not_qualified")

    def test_majority_mode_stays_in_progress_until_required_answers_complete(self):
        result = evaluate_qualification(
            runtime_policy=self._policy(),
            projected_state=self._state(True, True, None),
        )
        self.assertEqual(result["outcome"], "in_progress")


class StageTransitionPolicyTests(SimpleTestCase):
    def _context(self, description):
        return SimpleNamespace(
            conversation={
                "messages": [
                    {
                        "id": "message-1",
                        "direction": "inbound",
                        "body": "I want to speak with a person",
                    }
                ]
            },
            pipeline={
                "attribute_definitions": [],
                "available_stages": [
                    {
                        "id": "current-stage",
                        "name": "In Conversation",
                        "description": "Current working stage",
                        "config": {},
                    },
                    {
                        "id": "human-stage",
                        "name": "Human Intervention",
                        "description": description,
                        "config": {},
                    },
                    {
                        "id": "qualified-stage",
                        "name": "Qualified",
                        "description": "Move here when qualification passes",
                        "config": {},
                    },
                ],
            },
            stage={"id": "current-stage", "name": "In Conversation"},
        )

    def _decision(self, stage_id):
        return SimpleNamespace(
            qualification_updates=[],
            crm_actions=[
                {
                    "type": "pipeline_transition",
                    "stage_shift": {"stage_id": stage_id},
                }
            ],
        )

    def test_model_stage_transition_is_allowed_for_described_destination(self):
        actions, result = build_controlled_actions(
            decision=self._decision("human-stage"),
            context=self._context("Move here when the lead asks for human assistance."),
            runtime_policy={"qualification": {"criteria": []}},
            qualification_state={"requirement_states": {}},
            requirements=[],
        )
        self.assertIn(
            {
                "type": "pipeline_transition",
                "stage_shift": {"stage_id": "human-stage"},
            },
            actions,
        )
        self.assertEqual(result["stage_transition"]["source"], "stage_description")

    def test_undescribed_destination_is_not_ai_moveable(self):
        actions, _ = build_controlled_actions(
            decision=self._decision("human-stage"),
            context=self._context(""),
            runtime_policy={"qualification": {"criteria": []}},
            qualification_state={"requirement_states": {}},
            requirements=[],
        )
        self.assertFalse(any(item["type"] == "pipeline_transition" for item in actions))

    def test_model_cannot_bypass_deterministic_qualified_stage(self):
        actions, _ = build_controlled_actions(
            decision=self._decision("qualified-stage"),
            context=self._context("Move here when the lead asks for human assistance."),
            runtime_policy={"qualification": {"criteria": []}},
            qualification_state={"requirement_states": {}},
            requirements=[],
        )
        self.assertFalse(any(item["type"] == "pipeline_transition" for item in actions))


class KnowledgeRoutingTests(SimpleTestCase):
    def _context(self, body):
        return SimpleNamespace(
            conversation={
                "messages": [
                    {"id": "1", "direction": "inbound", "body": body}
                ]
            }
        )

    def test_short_guided_file_intents_trigger_rag(self):
        service = EngagementService.__new__(EngagementService)
        for text in ("catalog", "catalogue", "brochure", "pdf", "price"):
            with self.subTest(text=text):
                self.assertTrue(
                    service._should_retrieve_knowledge(context=self._context(text))
                )


class QualificationAntiRepeatTests(SimpleTestCase):
    def setUp(self):
        self.service = EngagementService.__new__(EngagementService)
        self.requirements = [
            {
                "id": "city",
                "label": "Which city are you located in",
                "question": "Which city are you located in?",
                "required": True,
                "priority": 1,
            },
            {
                "id": "timeline",
                "label": "When do you plan to buy",
                "question": "When do you plan to buy?",
                "required": True,
                "priority": 2,
            },
        ]

    def _context(self, body="Gurugram"):
        return SimpleNamespace(
            organization={
                "qualification_requirements": "Which city are you located in?\nWhen do you plan to buy?",
                "engagement_instructions": "",
            },
            conversation={
                "messages": [
                    {"id": "msg-2", "direction": "inbound", "body": body}
                ]
            },
        )

    def _state(self, include_timeline=True):
        states = {
            "city": {
                "status": "answered",
                "value": "Gurugram",
                "source_message_id": "msg-2",
            }
        }
        if include_timeline:
            states["timeline"] = {
                "status": "unknown",
                "value": None,
                "source_message_id": None,
            }
        return {
            "last_asked_requirement_id": "city",
            "requirement_states": states,
        }

    def test_repeating_just_answered_question_is_rejected(self):
        decision = SimpleNamespace(
            should_engage=True,
            silence_rule=None,
            qualification_updates=[],
            next_requirement_id="timeline",
            reason_code="QUALIFICATION_NEXT",
            message="Which city are you located in?",
        )
        with self.assertRaises(EngagementError):
            self.service._validate_qualification_decision(
                decision=decision,
                context=self._context(),
                requirements=self.requirements,
                qualification_state=self._state(),
            )

    def test_final_answer_accepts_acknowledgment_without_more_questions(self):
        decision = SimpleNamespace(
            should_engage=True,
            silence_rule=None,
            qualification_updates=[],
            next_requirement_id=None,
            reason_code="NORMAL_CONVERSATION",
            message="Thank you, I have all the required details.",
        )
        self.service._validate_qualification_decision(
            decision=decision,
            context=self._context(),
            requirements=[self.requirements[0]],
            qualification_state=self._state(include_timeline=False),
        )

    def test_final_answer_rejects_another_question(self):
        decision = SimpleNamespace(
            should_engage=True,
            silence_rule=None,
            qualification_updates=[],
            next_requirement_id=None,
            reason_code="NORMAL_CONVERSATION",
            message="Thank you. Is there anything else?",
        )
        with self.assertRaises(EngagementError):
            self.service._validate_qualification_decision(
                decision=decision,
                context=self._context(),
                requirements=[self.requirements[0]],
                qualification_state=self._state(include_timeline=False),
            )
