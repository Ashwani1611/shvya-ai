import json
from types import SimpleNamespace
from unittest.mock import Mock

from django.core.cache import cache
from django.test import SimpleTestCase

from apps.ai_engagement.services.ai_provider import AITextResult
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
            qualification_requirements=self.raw,
            engagement_instructions="Ask one qualification question at a time.",
            bump_up_enabled=False,
            bump_up_count=0,
        )

        first = self.requirements[0]
        self.provider = Mock()
        self.provider.generate_text.return_value = AITextResult(
            json.dumps(
                {
                    "should_engage": True,
                    "silence_rule": None,
                    "message": first["question"],
                    "file_document_id": None,
                    "crm_actions": [],
                    "qualification_updates": [],
                    "next_requirement_id": first["id"],
                    "reason_code": "QUALIFICATION_NEXT",
                }
            ),
            "test-model",
        )

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

    def test_option_answers_advance_to_q5_without_new_provider_calls(self):
        first = self.service.run(
            organization=self.organization,
            session_id="screenshot-flow",
            message="Hi",
        )
        self.assertIn(self.requirements[0]["question"], first.response)
        self.assertEqual(self.provider.generate_text.call_count, 1)

        turns = [
            ("A", 1),
            ("B", 2),
            ("A", 3),
            ("A", 4),
        ]
        for answer, expected_index in turns:
            result = self.service.run(
                organization=self.organization,
                session_id="screenshot-flow",
                message=answer,
            )
            self.assertEqual(result.model, "deterministic")
            self.assertEqual(
                result.response,
                self.requirements[expected_index]["question"],
            )

        # Configured qualification text, language and engagement instructions are
        # intentionally present. They must not force exact option replies through
        # the provider merely to discover the backend-owned next requirement.
        self.assertEqual(self.provider.generate_text.call_count, 1)

    def test_yes_text_for_q4_advances_to_q5_without_provider(self):
        self.service.run(
            organization=self.organization,
            session_id="yes-flow",
            message="Hi",
        )
        for answer in ("A", "B", "A"):
            self.service.run(
                organization=self.organization,
                session_id="yes-flow",
                message=answer,
            )

        result = self.service.run(
            organization=self.organization,
            session_id="yes-flow",
            message="YES",
        )

        self.assertEqual(result.model, "deterministic")
        self.assertEqual(result.response, self.requirements[4]["question"])
        self.assertEqual(self.provider.generate_text.call_count, 1)
