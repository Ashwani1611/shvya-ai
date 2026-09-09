from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.ai_engagement.graph.evidence import check_grounding, select_chunks
from apps.ai_engagement.services.ai_provider import AIProviderPermanentError
from apps.ai_engagement.services.engagement import EngagementDecision
from apps.ai_engagement.services.summary_limits import compact, merge_summary


class EvidencePipelineTests(SimpleTestCase):
    def test_retrieval_rejects_invalid_and_weak_scores_and_deduplicates(self):
        chunks = [{"content": "verified", "similarity": value}
                  for value in [None, "bad", float("nan"), float("inf"), .1, .8, .9]]
        result = select_chunks(chunks, threshold=.38, limit=5)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["similarity"], .9)

    def test_retrieval_has_a_hard_context_budget(self):
        result = select_chunks([{"content": "x" * 20000, "similarity": .8}], threshold=.38, limit=5)
        self.assertEqual(len(result[0]["content"]), 12000)

    def test_initial_and_rolling_character_limits(self):
        self.assertEqual(len(compact("😀" * 600)), 500)
        result = merge_summary("a" * 500, "b" * 200)
        self.assertLessEqual(len(result), 500)
        self.assertEqual(len(result.split(" ")[-1]), 150)
        self.assertEqual(merge_summary("Budget: 50k", "Budget: 50k"), "Budget: 50k")
        self.assertEqual(merge_summary("Budget: 50k", ""), "Budget: 50k")

    def _state(self, *, reason_code="ANSWER_ORG_QUESTION"):
        decision = EngagementDecision(
            should_engage=True,
            message="It costs $1",
            file_document_id=None,
            crm_actions=[],
            reason=reason_code,
            reason_code=reason_code,
            model="test",
        )
        return {
            "decision": decision,
            "context": SimpleNamespace(organization={}, knowledge=[], conversation={"messages": []}),
            "organization": SimpleNamespace(id="org"),
            "lead": SimpleNamespace(id="lead"),
        }

    def test_grounding_skips_secondary_model_for_non_org_fact_turns(self):
        state = self._state(reason_code="QUALIFICATION_NEXT")
        with patch("apps.ai_engagement.graph.evidence.OpenAIProvider") as provider:
            result = check_grounding(state)
        provider.assert_not_called()
        self.assertTrue(result["grounding_approved"])
        self.assertNotIn("decision", result)

    def test_grounding_rejection_returns_safe_reply_instead_of_silence(self):
        state = self._state()
        with patch("apps.ai_engagement.graph.evidence.OpenAIProvider") as provider:
            provider.return_value.generate_text.return_value.text = '{"approved":false,"reason":"unsupported"}'
            result = check_grounding(state)
        self.assertFalse(result["grounding_approved"])
        self.assertEqual(result["decision"].reason_code, "UNKNOWN_INFORMATION")
        self.assertTrue(result["decision"].should_engage)
        self.assertIn("verified information", result["decision"].message)

    def test_grounding_provider_failure_returns_safe_reply(self):
        state = self._state()
        with patch("apps.ai_engagement.graph.evidence.OpenAIProvider") as provider:
            provider.return_value.generate_text.side_effect = AIProviderPermanentError("credits unavailable")
            result = check_grounding(state)
        self.assertFalse(result["grounding_approved"])
        self.assertEqual(result["decision"].reason_code, "UNKNOWN_INFORMATION")

    def test_grounding_approval_keeps_original_decision(self):
        state = self._state()
        with patch("apps.ai_engagement.graph.evidence.OpenAIProvider") as provider:
            provider.return_value.generate_text.return_value.text = '{"approved":true,"reason":"supported"}'
            result = check_grounding(state)
        self.assertTrue(result["grounding_approved"])
        self.assertNotIn("decision", result)
