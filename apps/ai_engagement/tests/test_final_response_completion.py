"""Bounded correction also applies after real effects have been resolved."""
import json
from dataclasses import replace
from time import monotonic
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.ai_engagement.graph.evidence import check_grounding, _safe_unknown_decision
from apps.ai_engagement.services.ai_provider import AIProviderPermanentError
from apps.ai_engagement.services.engagement import EngagementDecision
from apps.ai_engagement.services.evidence_recovery import Coverage, CoveragePart
from apps.ai_engagement.services.post_state_finalization_guard import _FINAL_LANGUAGE_ONLY


class FinalResponseCompletionTests(SimpleTestCase):
    def state(self):
        return {"organization": SimpleNamespace(id="org", settings={}), "lead": SimpleNamespace(id="lead"),
            "context": SimpleNamespace(organization={"name": "Example", "about": "We automate follow-ups.",
                "bot_languages": "Hindi", "ai_playbook": "Reply in Hindi."}, lead={}, stage={}, knowledge=[],
                conversation={"messages": []}), "latest_text": "What does your service do?", "started_at": monotonic(),
            "decision": EngagementDecision(should_engage=True, message="We automate follow-ups.",
                file_document_id=7, crm_actions=[], qualification_updates=[], reason="ANSWER_ORG_QUESTION", model="fixture")}

    def run_final(self, responses, state=None):
        state = state or self.state()
        token = _FINAL_LANGUAGE_ONLY.set(True)
        try:
            with patch("apps.ai_engagement.graph.evidence.OpenAIProvider") as provider:
                provider.return_value.generate_text.side_effect = [
                    item if isinstance(item, Exception) else SimpleNamespace(text=json.dumps(item)) for item in responses]
                result = check_grounding(state)
                calls = provider.return_value.generate_text.call_args_list
        finally:
            _FINAL_LANGUAGE_ONLY.reset(token)
        return result, calls

    def test_final_wording_correction_keeps_resolved_file_and_actions_frozen(self):
        state = self.state()
        result, calls = self.run_final([
            {"approved": False, "reason": "language_mismatch"},
            {"message": "हम फ़ॉलो-अप को स्वचालित करते हैं।", "file_document_id": 999,
             "crm_actions": [{"type": "create_reminder"}]},
            {"approved": True, "reason": "approved"}], state)
        self.assertEqual(len(calls), 3)
        self.assertTrue(result["grounding_approved"])
        self.assertEqual(result["decision"].file_document_id, 7)
        self.assertEqual(result["decision"].crm_actions, [])
        self.assertEqual(result["decision"].qualification_updates, [])
        self.assertFalse(_FINAL_LANGUAGE_ONLY.get())

    def test_final_repair_cannot_approve_itself(self):
        result, calls = self.run_final([
            {"approved": False, "reason": "unanswered_question"},
            {"message": "Your booking is confirmed.", "approved": True},
            {"approved": False, "reason": "unperformed_action"}])
        self.assertEqual(len(calls), 3)
        self.assertFalse(result["grounding_approved"])
        self.assertIsNone(result["decision"].file_document_id)
        self.assertNotIn("confirmed", result["decision"].message)

    def test_final_repair_is_not_an_unbounded_loop(self):
        result, calls = self.run_final([
            {"approved": False, "reason": "language_mismatch"}, {"message": "Still wrong."},
            {"approved": False, "reason": "language_mismatch"}])
        self.assertFalse(result["grounding_approved"])
        self.assertEqual(len(calls), 3)
        self.assertIn("अभी", result["decision"].message)

    def test_final_unperformed_action_is_not_reauthorized(self):
        result, calls = self.run_final([{"approved": False, "reason": "unperformed_action"}])
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["decision"].crm_actions, [])
        self.assertIsNone(result["decision"].file_document_id)

    def test_final_timeout_obeys_existing_budget(self):
        state = self.state()
        state["started_at"] = monotonic() - 20
        result, calls = self.run_final([{"approved": False, "reason": "language_mismatch"}], state)
        self.assertEqual(len(calls), 1)
        self.assertFalse(result["grounding_approved"])

    def test_provider_error_is_localized_and_strips_unverified_effects(self):
        result, calls = self.run_final([AIProviderPermanentError("SECRET_PROVIDER_BODY")])
        self.assertEqual(len(calls), 1)
        self.assertIn("अभी", result["decision"].message)
        self.assertNotIn("SECRET", result["decision"].message)
        self.assertIsNone(result["decision"].file_document_id)

    def test_mixed_qualification_and_business_question_is_not_only_acknowledged(self):
        state = self.state()
        state["evidence_coverage"] = Coverage("partial", parts=(CoveragePart("Price?", False),))
        decision = _safe_unknown_decision(state["decision"], qualification_turn=True, state=state)
        self.assertEqual(decision.reason_code, "UNKNOWN_INFORMATION")
        self.assertNotIn("धन्यवाद", decision.message)

    def test_successful_final_answer_does_not_add_a_correction_call(self):
        result, calls = self.run_final([{"approved": True, "reason": "approved"}])
        self.assertEqual(len(calls), 1)
        self.assertTrue(result["grounding_approved"])

    def test_final_generic_unknown_can_use_existing_evidence(self):
        state = self.state()
        state["decision"] = replace(state["decision"], file_document_id=None,
            message="The team would need to confirm.", reason_code="UNKNOWN_INFORMATION")
        result, calls = self.run_final([{"approved": True, "reason": "approved"},
            {"message": "हम फ़ॉलो-अप को स्वचालित करते हैं।"}, {"approved": True, "reason": "approved"}], state)
        self.assertEqual(len(calls), 3)
        self.assertTrue(result["grounding_approved"])
        self.assertIn("फ़ॉलो-अप", result["decision"].message)

    def test_mixed_request_is_not_only_acknowledged_with_recovery_disabled(self):
        state = self.state()
        state["latest_text"] = "We use Excel. Please send me a brochure."
        state.pop("evidence_coverage", None)
        decision = _safe_unknown_decision(state["decision"], qualification_turn=True, state=state)
        self.assertEqual(decision.reason_code, "UNKNOWN_INFORMATION")
        self.assertNotIn("धन्यवाद", decision.message)
