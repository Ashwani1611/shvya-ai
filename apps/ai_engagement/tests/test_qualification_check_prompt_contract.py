import json

from django.test import SimpleTestCase

from apps.ai_engagement.prompts.qualification_check import (
    QUALIFICATION_CHECK_INSTRUCTIONS,
)
from apps.ai_engagement.services.qualification_check import QualificationCheckService


class QualificationCheckPromptContractTests(SimpleTestCase):
    def test_input_contains_ai_setup_fields_and_qualification_state(self):
        payload = json.loads(
            QualificationCheckService._build_input(
                {
                    "organization": {
                        "name": "Example Org",
                        "about": "About text",
                        "bot_languages": "English, Hindi",
                        "qualification_requirements": "Ask Q1 and Q2",
                        "engagement_instructions": "Qualify only when all questions are answered",
                    },
                    "lead": {
                        "id": "lead-1",
                        "qualification": {"status": "in_progress"},
                    },
                    "conversation": {
                        "messages": [
                            {
                                "direction": "inbound",
                                "body": "I already answered Q1",
                            }
                        ]
                    },
                    "conversation_summary": {"summary": "Lead answered Q1"},
                }
            )
        )

        self.assertEqual(payload["organization"]["about"], "About text")
        self.assertEqual(
            payload["organization"]["qualification_requirements"],
            "Ask Q1 and Q2",
        )
        self.assertEqual(
            payload["organization"]["engagement_instructions"],
            "Qualify only when all questions are answered",
        )
        self.assertEqual(
            payload["qualification_state"],
            {"status": "in_progress"},
        )
        self.assertEqual(
            payload["recent_conversation"]["messages"][0]["direction"],
            "inbound",
        )

    def test_prompt_preserves_all_questions_override_and_default_majority_rule(self):
        prompt = QUALIFICATION_CHECK_INSTRUCTIONS.casefold()

        self.assertIn("all-questions override", prompt)
        self.assertIn("default majority rule", prompt)
        self.assertIn("strictly more than half", prompt)
        self.assertIn("qualification_requirements", prompt)
        self.assertIn("engagement_instructions", prompt)
        self.assertIn("all_questions_answered", prompt)

    def test_prompt_counts_volunteered_information_as_answered(self):
        prompt = QUALIFICATION_CHECK_INSTRUCTIONS.casefold()

        self.assertIn("does not need to have been explicitly asked first", prompt)
        self.assertIn("volunteered information", prompt)
