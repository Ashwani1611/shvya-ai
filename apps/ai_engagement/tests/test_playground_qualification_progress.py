from tests.playbook_fixtures import build_ai_playbook

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.test import SimpleTestCase

from apps.ai_engagement.services.ai_provider import AITextResult
from apps.ai_engagement.services.engagement import EngagementDecision
from apps.ai_engagement.services.organization_profile import (
    compile_qualification_requirements,
)
from apps.ai_engagement.services.playground import PlaygroundService


class PlaygroundQualificationProgressRegressionTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.raw = (
            "Q1. What is your biggest challenge with managing or converting leads right now? "
            "A. Slow replies B. Missed follow-ups C. Leads going cold D. No proper tracking\n"
            "Q2. Where do you currently manage your leads? "
            "A. WhatsApp chats B. Excel / Sheets C. CRM D. Multiple places\n"
            "Q3. How many leads do you typically receive per day? "
            "A. 0-10 B. 10-30 C. 30+\n"
            "Q4. Do you currently run ads? A. Yes B. No\n"
            "Q5. Where do most of your leads come from today? "
            "A. Referrals B. Meta ads C. Organic / social D. Other"
        )
        self.requirements = compile_qualification_requirements(self.raw)[
            "requirements"
        ]

        self.org_info = Mock()
        self.org_info.get_or_create.return_value = SimpleNamespace(
            ai_enabled=True,
            about="Sales engagement platform",
            bot_languages="English",
            ai_playbook=build_ai_playbook(questions=self.raw, rules="Ask one qualification question at a time."),

            bump_up_enabled=False,
            bump_up_count=0,
        )

        self.provider = Mock()
        self._set_provider_to_fail_after_q4()

        retrieval = Mock()
        retrieval.retrieve_by_vector.return_value = []
        self.service = PlaygroundService(
            provider=self.provider,
            org_info_service=self.org_info,
            embedding_service=Mock(),
            retrieval_service=retrieval,
        )
        self.organization = SimpleNamespace(
            id="sandbox-q4-regression",
            name="Shvya Test",
        )

    def _provider_result_for_requirement(self, index):
        requirement = self.requirements[index]
        return AITextResult(
            json.dumps(
                {
                    "should_engage": True,
                    "silence_rule": None,
                    "message": requirement["question"],
                    "file_document_id": None,
                    "crm_actions": [],
                    "qualification_updates": [],
                    "next_requirement_id": requirement["id"],
                    "reason_code": "QUALIFICATION_NEXT",
                }
            ),
            "test-model",
        )

    def _set_provider_to_fail_after_q4(self):
        self.provider.generate_text.side_effect = [
            self._provider_result_for_requirement(0),
            self._provider_result_for_requirement(1),
            self._provider_result_for_requirement(2),
            self._provider_result_for_requirement(3),
            RuntimeError("simulated provider/schema failure after Q4"),
        ]

    def _start_and_reach_q4(self, session_id):
        first = self.service.run(
            organization=self.organization,
            session_id=session_id,
            message="Hi",
        )
        self.assertIn(self.requirements[0]["question"], first.response)

        for answer, expected_index in (("A", 1), ("B", 2), ("A", 3)):
            result = self.service.run(
                organization=self.organization,
                session_id=session_id,
                message=answer,
            )
            self.assertIn(self.requirements[expected_index]["question"], result.response)

    def _assert_q5_recovery(self, result):
        self.assertEqual(result.model, "deterministic-recovery")
        self.assertTrue(result.response.startswith("Nice."))
        self.assertIn(self.requirements[4]["question"], result.response)

    def test_q4_option_answer_recovers_to_q5_when_generation_fails(self):
        self._start_and_reach_q4("screenshot-flow")

        result = self.service.run(
            organization=self.organization,
            session_id="screenshot-flow",
            message="A",
        )

        self._assert_q5_recovery(result)

    def test_q4_yes_text_recovers_to_q5_when_generation_fails(self):
        self._start_and_reach_q4("yes-flow")

        result = self.service.run(
            organization=self.organization,
            session_id="yes-flow",
            message="YES",
        )

        self._assert_q5_recovery(result)

    def test_q4_natural_yes_text_recovers_to_q5_when_generation_fails(self):
        self._start_and_reach_q4("natural-yes-flow")

        result = self.service.run(
            organization=self.organization,
            session_id="natural-yes-flow",
            message="YES, I RUN ADS",
        )

        self._assert_q5_recovery(result)

    def test_information_intent_uses_grounded_fallback_instead_of_repeating_q1(self):
        messages = (
            "price of plan",
            "what all pack shvya offer?",
            "what is shvya and its features",
            "pricer coif npcs",
        )

        for index, message in enumerate(messages, start=1):
            with self.subTest(message=message):
                self.provider.generate_text.side_effect = RuntimeError(
                    "simulated provider/schema failure on information request"
                )
                fallback = EngagementDecision(
                    should_engage=True,
                    message="Verified plan information fallback.",
                    file_document_id=None,
                    crm_actions=[],
                    reason="ANSWER_ORG_QUESTION",
                    reason_code="ANSWER_ORG_QUESTION",
                    model="deterministic-fallback",
                )
                with patch.object(
                    self.service,
                    "_fallback_decision",
                    return_value=fallback,
                ) as fallback_decision:
                    result = self.service.run(
                        organization=self.organization,
                        session_id=f"info-intent-{index}",
                        message=message,
                    )

                fallback_decision.assert_called_once()
                self.assertIn("Verified plan information fallback.", result.response)
                self.assertNotIn(self.requirements[0]["question"], result.response)
