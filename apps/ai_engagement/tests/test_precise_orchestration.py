import json
from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.ai_engagement.prompts import (
    CUSTOMER_ENGAGEMENT_INSTRUCTIONS,
    INTERNAL_CONVERSATION_SUMMARY_INSTRUCTIONS,
)
from apps.ai_engagement.services.ai_provider import AITextResult
from apps.ai_engagement.services.context import AIContext
from apps.ai_engagement.services.engagement import EngagementError, EngagementService


class _SequenceProvider:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def generate_text(self, **kwargs):
        self.calls.append(kwargs)
        if not self.outputs:
            raise AssertionError("provider called more times than expected")
        return AITextResult(
            text=self.outputs.pop(0),
            model="test-model",
        )


class PreciseEngagementTests(SimpleTestCase):
    def _context(self, latest="Yes"):
        return AIContext(
            organization={
                "id": "org-1",
                "name": "Example Org",
                "about": "Example business",
                "bot_languages": "English",
                "qualification_requirements": "Budget and timeline",
                "engagement_instructions": "Be concise",
                "ai_enabled": True,
                "bump_up_enabled": False,
                "bump_up_count": 0,
            },
            lead={
                "id": "lead-1",
                "name": "Lead",
                "phone": "",
                "email": "",
                "notes": "",
                "attributes": {},
                "lead_source": "whatsapp",
                "stage_entered_at": None,
                "created_at": None,
                "updated_at": None,
            },
            pipeline={},
            stage={},
            contacts=[],
            attributes=[],
            conversation={
                "message_count": 1,
                "messages": [
                    {
                        "id": "",
                        "direction": "inbound",
                        "speaker": "lead",
                        "body": latest,
                        "created_at": None,
                    }
                ],
            },
            conversation_summary=None,
            qualification_notes=[],
            knowledge=[],
        )

    def test_short_qualification_answer_skips_rag(self):
        service = EngagementService(provider=_SequenceProvider([]))
        self.assertFalse(
            service._should_retrieve_knowledge(context=self._context("Yes"))
        )
        self.assertFalse(
            service._should_retrieve_knowledge(context=self._context("Option A"))
        )
        self.assertFalse(
            service._should_retrieve_knowledge(context=self._context("20000000"))
        )

    def test_customer_question_can_trigger_rag(self):
        service = EngagementService(provider=_SequenceProvider([]))
        self.assertTrue(
            service._should_retrieve_knowledge(
                context=self._context("What does your premium package include?")
            )
        )

    def test_malformed_json_gets_exactly_one_repair(self):
        valid = json.dumps(
            {
                "should_engage": True,
                "message": "What timeline are you considering?",
                "file_document_id": None,
                "crm_actions": [],
                "reason": "timeline remains unresolved",
            }
        )
        provider = _SequenceProvider(["not-json", valid])
        service = EngagementService(provider=provider)

        result = service.engage(
            organization=SimpleNamespace(id="org-1"),
            lead=SimpleNamespace(id="lead-1"),
            context=self._context("Around 2 crore"),
        )

        self.assertTrue(result.should_engage)
        self.assertEqual(result.message, "What timeline are you considering?")
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(provider.calls[0]["metadata"]["phase"], "primary")
        self.assertEqual(provider.calls[1]["metadata"]["phase"], "schema_repair")

    def test_invalid_repair_is_not_retried_again(self):
        provider = _SequenceProvider(["not-json", "still-not-json"])
        service = EngagementService(provider=provider)

        with self.assertRaises(EngagementError):
            service.engage(
                organization=SimpleNamespace(id="org-1"),
                lead=SimpleNamespace(id="lead-1"),
                context=self._context("Hello"),
            )

        self.assertEqual(len(provider.calls), 2)

    def test_prompt_forbids_chain_of_thought_and_duplicate_questions(self):
        self.assertIn("Never ask for information that is already present", CUSTOMER_ENGAGEMENT_INSTRUCTIONS)
        self.assertIn("Do not include chain-of-thought", CUSTOMER_ENGAGEMENT_INSTRUCTIONS)
        self.assertIn("Deduplicate facts", INTERNAL_CONVERSATION_SUMMARY_INSTRUCTIONS)
