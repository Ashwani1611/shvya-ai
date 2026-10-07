"""Affirmative handoff wording keeps action priority over business enquiries."""
from django.test import SimpleTestCase

from apps.ai_engagement.services.intent_rules import deterministic_intents, ordered_intents
from apps.ai_engagement.services.intent_types import Intent


class ExplicitHandoffWordingTests(SimpleTestCase):
    def test_phone_call_request_wording(self):
        for text in (
            "Please arrange a phone call with a human about General Training. I am not booking a trial yet.",
            "Could you arrange a phone call about the membership fee?",
            "Can you please schedule a telephone call?",
            "I would like a phone call about the price.",
        ):
            with self.subTest(text=text):
                intents = deterministic_intents(text)
                self.assertIn(Intent.CALL_REQUEST, intents)
                self.assertEqual(ordered_intents(intents)[0], Intent.CALL_REQUEST)

    def test_human_assistance_request_wording(self):
        for text in (
            "I want to connect for human assistance.",
            "Please connect me to a human about your fees.",
            "Could you connect me with a team member?",
            "I need to speak with a consultant about the price.",
        ):
            with self.subTest(text=text):
                intents = deterministic_intents(text)
                self.assertIn(Intent.HUMAN_REQUEST, intents)
                self.assertEqual(ordered_intents(intents)[0], Intent.HUMAN_REQUEST)

    def test_informational_negated_and_quoted_mentions_do_not_add_requests(self):
        for text in (
            "Do you offer a phone call?",
            "Can you explain how a phone call works?",
            "I do not want a phone call.",
            "Please do not arrange a phone call.",
            "I do not want to connect for human assistance.",
            "The brochure says please arrange a phone call.",
            "Yesterday I asked you to arrange a phone call.",
        ):
            with self.subTest(text=text):
                intents = deterministic_intents(text)
                self.assertNotIn(Intent.CALL_REQUEST, intents)
                self.assertNotIn(Intent.HUMAN_REQUEST, intents)
