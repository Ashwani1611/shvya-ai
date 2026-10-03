from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.ai_engagement.services.ai_provider import OpenAIProvider
from apps.ai_engagement.services.engagement import EngagementService
from apps.ai_engagement.services.response_fallbacks import fallback_message
from apps.ai_engagement.services.turn_burst import turn_burst_seconds
from apps.ai_engagement.services.turn_controller import (
    QUALIFICATION_MODE,
    SALES_SUPPORT_MODE,
    build_business_plan,
    build_turn_policy,
)


class SmoothTurnControllerTests(SimpleTestCase):
    def _context(self, *, stage="New Lead", knowledge=None):
        return SimpleNamespace(
            organization={
                "ai_playbook": "Rules\nAnswer customer questions first.\nQualification Questions\nQ1. Volume?",
                "qualification_model": "gpt-qualification-org",
                "sales_support_model": "gpt-sales-org",
            },
            stage={"name": stage},
            knowledge=list(knowledge or []),
        )

    def test_new_lead_uses_qualification_mode_and_org_model(self):
        policy = build_turn_policy(
            context=self._context(stage="New Lead"),
            qualification_state={"engagement_mode": "qualification"},
        )
        self.assertEqual(policy.prompt_mode, QUALIFICATION_MODE)
        self.assertEqual(policy.model_override, "gpt-qualification-org")
        self.assertIn("Qualification Questions", policy.operating_spec)

    def test_other_stage_uses_sales_support_mode_and_org_model(self):
        policy = build_turn_policy(
            context=self._context(stage="Qualified"),
            qualification_state={"engagement_mode": "conversation"},
        )
        self.assertEqual(policy.prompt_mode, SALES_SUPPORT_MODE)
        self.assertEqual(policy.model_override, "gpt-sales-org")

    def test_business_plan_answers_question_before_next_qualification(self):
        context = self._context(
            knowledge=[{"chunk_id": "1", "content": "Plan details", "similarity": 0.9}]
        )
        service = SimpleNamespace(
            _should_retrieve_knowledge=lambda **kwargs: True,
        )
        policy = build_turn_policy(
            context=context,
            qualification_state={"engagement_mode": "qualification"},
        )
        plan = build_business_plan(
            service=service,
            context=context,
            qualification_state={
                "engagement_mode": "qualification",
                "requirement_states": {},
            },
            requirements=[
                {
                    "id": "volume",
                    "question": "How many leads do you receive?",
                    "priority": 1,
                    "required": True,
                }
            ],
            latest_text="What does it cost?",
            turn_policy=policy,
        )
        self.assertEqual(plan["priority"], "answer_then_qualify")
        self.assertTrue(plan["answer_customer_first"])
        self.assertEqual(plan["next_requirement_id"], "volume")

    def test_engagement_uses_top_five_retrieval_results(self):
        self.assertEqual(EngagementService.KNOWLEDGE_LIMIT, 5)

    def test_provider_honors_turn_model_override(self):
        provider = OpenAIProvider.__new__(OpenAIProvider)
        provider._explicit_model = False
        provider.model = "platform-default"
        self.assertEqual(
            provider._model_for_metadata(
                {"task": "engagement", "model_override": "gpt-org-override"}
            ),
            "gpt-org-override",
        )

    def test_default_burst_window_is_four_seconds(self):
        with patch.dict("os.environ", {}, clear=False):
            # Remove legacy/explicit values if the test runner exported them.
            import os

            old_turn = os.environ.pop("AI_TURN_BURST_SECONDS", None)
            old_legacy = os.environ.pop("AI_ENGAGEMENT_DEBOUNCE_SECONDS", None)
            try:
                self.assertEqual(turn_burst_seconds(), 4)
            finally:
                if old_turn is not None:
                    os.environ["AI_TURN_BURST_SECONDS"] = old_turn
                if old_legacy is not None:
                    os.environ["AI_ENGAGEMENT_DEBOUNCE_SECONDS"] = old_legacy

    def test_burst_window_is_bounded_to_five_seconds(self):
        with patch.dict("os.environ", {"AI_TURN_BURST_SECONDS": "30"}):
            self.assertEqual(turn_burst_seconds(), 5)

    def test_fallback_does_not_use_verified_information_refusal(self):
        for kind in ("technical", "unverified", "pricing", "policy"):
            with self.subTest(kind=kind):
                message = fallback_message(kind=kind, bot_languages="English")
                self.assertNotIn("verified information", message.casefold())
                self.assertNotIn("enough verified", message.casefold())
