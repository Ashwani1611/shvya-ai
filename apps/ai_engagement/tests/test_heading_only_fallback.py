from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.ai_engagement.services.engagement import EngagementDecision
from apps.ai_engagement.services.engagement_failsoft import _grounded_conversation_reply
from apps.ai_engagement.services.first_inbound_welcome_runtime import apply_first_inbound_welcome


class HeadingOnlyFallbackTests(SimpleTestCase):
    about = """SHVYA AI helps businesses manage leads.

## Plans and Pricing

- DIY Plan:
₹2,999/month per user.

- Done For You Plan:
₹14,999 for the first month, then ₹2,999/month per user.

- Enterprise Plan:
₹39,999/month, then ₹2,999/month per user.

## Contact
Contact our team for assistance.
"""

    def test_screenshot_questions_include_all_prices_and_conditions(self):
        for question in ("Plans and Pricing", "what all Plans and Pricing shvya have"):
            with self.subTest(question=question):
                message, reason = _grounded_conversation_reply(
                    about=self.about, inbound=question, organization_name="Shvya AI",
                )
                self.assertEqual(reason, "ANSWER_ORG_QUESTION")
                self.assertIn("₹2,999/month per user", message)
                self.assertIn("₹14,999 for the first month", message)
                self.assertIn("₹39,999/month, then", message)
                self.assertIn("DIY Plan", message)
                self.assertNotIn("Contact our team", message)

    def test_heading_without_prices_is_not_an_answer(self):
        message, reason = _grounded_conversation_reply(
            about="SHVYA AI helps manage leads.\n## Plans and Pricing",
            inbound="Plans and Pricing", organization_name="Shvya AI",
        )
        self.assertEqual(reason, "UNKNOWN_INFORMATION")
        self.assertNotEqual(message, "## Plans and Pricing")

    def test_first_information_answer_does_not_get_qualification_welcome(self):
        decision = EngagementDecision(
            should_engage=True, message="DIY is ₹2,999/month per user.",
            file_document_id=None, crm_actions=[], reason="ANSWER_ORG_QUESTION",
            reason_code="ANSWER_ORG_QUESTION", model="test",
        )
        result = apply_first_inbound_welcome(
            decision=decision, organization=SimpleNamespace(name="Shvya AI"),
            lead=SimpleNamespace(name="Ash", stage=SimpleNamespace(name="Nurturing")),
            first_turn=True,
        )
        self.assertEqual(result.message, decision.message)

    def test_fallback_preserves_plan_exceptions_and_does_not_invent_prices(self):
        about = "## Pricing\nStarter is ₹999/month.\nNo refunds after activation.\nTaxes are extra."
        message, _ = _grounded_conversation_reply(
            about=about, inbound="pricing", organization_name="Shvya AI",
        )
        self.assertIn("No refunds after activation.", message)
        self.assertIn("Taxes are extra.", message)
