"""Provider compaction preserves available current-turn budget."""
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.ai_engagement.services.ai_provider import OpenAIProvider


class ProviderPromptBudgetTests(SimpleTestCase):
    def bound(self, instructions, payload):
        provider = object.__new__(OpenAIProvider)
        with patch.object(provider, "_max_prompt_tokens", return_value=24000):
            return provider._bound_prompt(instructions=instructions, input_text=payload)

    def test_short_instructions_reclaim_input_budget_from_original(self):
        instructions = "SYSTEM_POLICY" + "s" * 1000
        payload = "a" * 70000 + "CURRENT_REQUIREMENT_MARKER" + "b" * 30000
        bounded_instructions, bounded_input, compacted = self.bound(instructions, payload)
        self.assertTrue(compacted)
        self.assertEqual(bounded_instructions, instructions)
        self.assertIn("CURRENT_REQUIREMENT_MARKER", bounded_input)
        self.assertEqual(len(bounded_instructions) + len(bounded_input), 96000)
        self.assertEqual(bounded_input.count("[SHVYA runtime prompt compacted"), 1)

    def test_large_instructions_keep_complete_small_turn_payload(self):
        instructions = "SYSTEM_HEAD" + "s" * 100000 + "SYSTEM_TAIL"
        payload = '{"current_message":"hello","next_requirement":{"id":"goal"}}'
        bounded_instructions, bounded_input, compacted = self.bound(instructions, payload)
        self.assertTrue(compacted)
        self.assertEqual(bounded_input, payload)
        self.assertTrue(bounded_instructions.startswith("SYSTEM_HEAD"))
        self.assertTrue(bounded_instructions.endswith("SYSTEM_TAIL"))
        self.assertEqual(len(bounded_instructions) + len(bounded_input), 96000)

    def test_request_inside_budget_is_unchanged(self):
        instructions, payload = "Approved system policy", '{"next_requirement":{"id":"goal"}}'
        self.assertEqual(self.bound(instructions, payload), (instructions, payload, False))
