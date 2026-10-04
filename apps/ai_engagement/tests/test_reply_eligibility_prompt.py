import unittest

from apps.ai_engagement.prompts.engagement import CUSTOMER_ENGAGEMENT_INSTRUCTIONS


class ReplyEligibilityPromptTests(unittest.TestCase):
    def test_generation_contract_matches_backend_silence_guard(self):
        prompt = CUSTOMER_ENGAGEMENT_INSTRUCTIONS
        self.assertIn("should_engage=true, silence_rule=null", prompt)
        self.assertIn("including after the final answer", prompt)
        self.assertNotIn("instruction may additionally require silence", prompt)
        self.assertNotIn('If should_engage is false, message MUST be ""', prompt)
