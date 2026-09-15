import json

from django.test import SimpleTestCase

from apps.ai_engagement.services import transactional_turn_runtime as runtime
from apps.ai_engagement.services.ai_provider import AITextResult
from apps.ai_engagement.services.engagement import EngagementService


class PostStateFinalizationGuardTests(SimpleTestCase):
    def _result(self):
        return AITextResult(
            json.dumps(
                {
                    "should_engage": True,
                    "message": "Thanks, your details are updated.",
                    "file_document_id": None,
                    "crm_actions": [
                        {"type": "add_note", "note": "Already resolved."}
                    ],
                    "qualification_updates": [
                        {
                            "requirement_id": "which_city",
                            "value": "Delhi",
                            "source_message_id": "inbound-1",
                            "evidence": "Delhi",
                        }
                    ],
                    "next_requirement_id": "what_occupation",
                    "reason_code": "QUALIFICATION_NEXT",
                }
            ),
            "test-model",
        )

    def test_final_post_state_normalization_is_language_only(self):
        token = runtime._PRECOMPUTED_DECISION.set(
            {
                "lead_id": "lead-1",
                "source_message_id": "inbound-1",
                "force_regenerate": True,
            }
        )
        try:
            decision = EngagementService()._normalize_result(result=self._result())
        finally:
            runtime._PRECOMPUTED_DECISION.reset(token)

        self.assertEqual(decision.crm_actions, [])
        self.assertEqual(decision.qualification_updates, [])
        self.assertEqual(decision.message, "Thanks, your details are updated.")
        self.assertEqual(decision.next_requirement_id, "what_occupation")

    def test_normal_generation_preserves_proposed_actions(self):
        token = runtime._PRECOMPUTED_DECISION.set(None)
        try:
            decision = EngagementService()._normalize_result(result=self._result())
        finally:
            runtime._PRECOMPUTED_DECISION.reset(token)

        self.assertEqual(len(decision.crm_actions), 1)
        self.assertEqual(len(decision.qualification_updates), 1)
