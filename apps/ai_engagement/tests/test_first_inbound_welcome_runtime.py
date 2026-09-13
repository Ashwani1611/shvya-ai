from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.ai_engagement.services.engagement import EngagementDecision
from apps.ai_engagement.services.first_inbound_welcome_runtime import (
    apply_first_inbound_welcome,
)


class FirstInboundWelcomeRuntimeTests(SimpleTestCase):
    def _decision(self, message):
        return EngagementDecision(
            should_engage=True,
            message=message,
            file_document_id=None,
            crm_actions=[],
            reason="QUALIFICATION_NEXT",
            reason_code="QUALIFICATION_NEXT",
            next_requirement_id="q1",
            model="test",
        )

    def test_first_reply_greets_by_first_name_then_preserves_q1(self):
        question = (
            'Q1. "What is your biggest challenge with managing or converting leads right now?"\n'
            "A. Slow replies\n"
            "B. Missed follow-ups\n"
            "C. Leads going cold\n"
            "D. No proper tracking"
        )
        result = apply_first_inbound_welcome(
            decision=self._decision(question),
            organization=SimpleNamespace(name="MiM-Essay"),
            lead=SimpleNamespace(name="Gaurav Sharma"),
            first_turn=True,
        )

        self.assertTrue(
            result.message.startswith(
                "Hi Gaurav! Thanks for reaching out to MiM-Essay.\n\nQ1."
            )
        )
        for option in (
            "A. Slow replies",
            "B. Missed follow-ups",
            "C. Leads going cold",
            "D. No proper tracking",
        ):
            self.assertIn(option, result.message)

    def test_generic_or_phone_name_uses_hi_there(self):
        result = apply_first_inbound_welcome(
            decision=self._decision("What service are you interested in?"),
            organization=SimpleNamespace(name="SHVYA AI"),
            lead=SimpleNamespace(name="WhatsApp Lead"),
            first_turn=True,
        )
        self.assertEqual(
            result.message,
            "Hi there! Thanks for reaching out to SHVYA AI.\n\n"
            "What service are you interested in?",
        )

    def test_existing_greeting_is_not_duplicated(self):
        original = self._decision(
            "Hi Gaurav! Thanks for reaching out to SHVYA AI.\n\nWhat is your goal?"
        )
        result = apply_first_inbound_welcome(
            decision=original,
            organization=SimpleNamespace(name="SHVYA AI"),
            lead=SimpleNamespace(name="Gaurav"),
            first_turn=True,
        )
        self.assertEqual(result.message, original.message)

    def test_later_turn_is_not_greeted_again(self):
        original = self._decision("What is your next requirement?")
        result = apply_first_inbound_welcome(
            decision=original,
            organization=SimpleNamespace(name="SHVYA AI"),
            lead=SimpleNamespace(name="Gaurav"),
            first_turn=False,
        )
        self.assertEqual(result.message, original.message)
