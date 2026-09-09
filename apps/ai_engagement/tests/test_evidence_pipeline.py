from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.ai_engagement.graph.evidence import check_grounding, select_chunks
from apps.ai_engagement.services.engagement import EngagementError
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

    def test_grounding_gate_fails_closed(self):
        state = {"decision": SimpleNamespace(should_engage=True, model="test", message="It costs $1",
                                             next_requirement_id=None),
                 "context": SimpleNamespace(organization={}, knowledge=[]),
                 "organization": SimpleNamespace(id="org"), "lead": SimpleNamespace(id="lead")}
        with patch("apps.ai_engagement.graph.evidence.OpenAIProvider") as provider:
            for verdict in ['{"approved":false}', 'bad json', '{"approved":"true"}']:
                provider.return_value.generate_text.return_value.text = verdict
                with self.assertRaises(EngagementError):
                    check_grounding(state)
            provider.return_value.generate_text.return_value.text = '{"approved":true}'
            self.assertTrue(check_grounding(state)["grounding_approved"])

