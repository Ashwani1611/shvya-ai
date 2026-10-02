import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.ai_engagement.graph.evidence import check_grounding
from apps.ai_engagement.services.ai_provider import AIProviderPermanentError
from apps.ai_engagement.services.engagement import EngagementDecision


class GroundingReplyRepairTests(SimpleTestCase):
    def test_deterministic_label_does_not_bypass_configured_language_validation(self):
        state = self.state()
        state["requirements"] = [{"id": "budget", "question": "What is your budget?"}]
        state["qualification_state"] = {"engagement_mode": "qualification", "requirement_states": {}}
        state["decision"] = EngagementDecision(
            should_engage=True, message="What is your budget?", file_document_id=None,
            crm_actions=[], reason="QUALIFICATION_NEXT", reason_code="QUALIFICATION_NEXT",
            next_requirement_id="budget", model="deterministic",
        )
        corrected = "आपका बजट कितना है?"
        with patch("apps.ai_engagement.graph.evidence.OpenAIProvider") as provider:
            provider.return_value.generate_text.side_effect = [
                SimpleNamespace(text=json.dumps(item)) for item in (
                    {"approved": False, "reason": "language_mismatch"},
                    {"message": corrected}, {"approved": True, "reason": "approved"},
                )
            ]
            result = check_grounding(state)
        self.assertEqual(provider.return_value.generate_text.call_count, 3)
        self.assertTrue(result["grounding_approved"])
        self.assertEqual(result["decision"].message, corrected)
        self.assertEqual(result["decision"].next_requirement_id, "budget")

    def state(self):
        return {
            "organization": SimpleNamespace(id="org-a", settings={}),
            "lead": SimpleNamespace(id="lead-a"),
            "context": SimpleNamespace(
                organization={"name": "Example", "about": "We automate follow-ups.",
                              "bot_languages": "Hindi", "ai_playbook": "## Rules\nAlways answer in Hindi."},
                lead={}, stage={}, knowledge=[], conversation={"messages": []}),
            "latest_text": "What does your company do?",
            "decision": EngagementDecision(should_engage=True, message="We automate follow-ups.",
                file_document_id=7, crm_actions=[], reason="ANSWER_ORG_QUESTION", model="test"),
        }

    def run_guard(self, responses):
        state = self.state()
        with patch("apps.ai_engagement.graph.evidence.OpenAIProvider") as provider:
            provider.return_value.generate_text.side_effect = [
                item if isinstance(item, Exception) else SimpleNamespace(text=json.dumps(item))
                for item in responses
            ]
            with patch("apps.ai_engagement.services.trace_service.record") as trace:
                result = check_grounding(state)
        return state, result, provider.return_value.generate_text.call_args_list, trace

    def test_wrong_language_is_corrected_and_independently_revalidated(self):
        corrected = "हम फ़ॉलो-अप को स्वचालित करते हैं।"
        state, result, calls, trace = self.run_guard([
            {"approved": False, "reason": "language_mismatch"}, {"message": corrected},
            {"approved": True, "reason": "approved"},
        ])
        self.assertTrue(result["grounding_approved"])
        self.assertEqual(result["decision"].message, corrected)
        self.assertEqual(result["decision"].file_document_id, state["decision"].file_document_id)
        self.assertEqual(len(calls), 3)
        first, repair, final = [json.loads(call.kwargs["input_text"]) for call in calls]
        self.assertEqual(repair["bot_languages"], "Hindi")
        self.assertEqual(first["organization_facts"], final["organization_facts"])
        self.assertEqual(final["reply"], corrected)
        trace.assert_called_with("grounding", {
            "approved": True, "validation_reason": "approved", "repair_attempted": True})

    def test_repair_cannot_self_approve_or_add_actions(self):
        _, result, calls, _ = self.run_guard([
            {"approved": False, "reason": "instruction_disclosure"},
            {"message": "Your booking is confirmed.", "approved": True, "file_document_id": 99},
            {"approved": False, "reason": "unperformed_action"},
        ])
        self.assertEqual(len(calls), 3)
        self.assertFalse(result["grounding_approved"])
        self.assertNotIn("confirmed", result["decision"].message)
        self.assertIsNone(result["decision"].file_document_id)
        self.assertEqual(result["decision"].crm_actions, [])

    def test_unsupported_file_or_facts_are_not_retried_as_wording_errors(self):
        for reason in ("invalid_file", "unsupported_claim", "invalid_qualification", "unperformed_action"):
            with self.subTest(reason=reason):
                _, result, calls, _ = self.run_guard([{"approved": False, "reason": reason}])
                self.assertFalse(result["grounding_approved"])
                self.assertEqual(len(calls), 1)

    def test_malformed_repair_and_provider_failure_fail_closed(self):
        for repair in ({"message": ""}, {"message": 123}, AIProviderPermanentError("unavailable")):
            with self.subTest(repair=repair):
                _, result, calls, _ = self.run_guard([
                    {"approved": False, "reason": "language_mismatch"}, repair,
                ])
                self.assertFalse(result["grounding_approved"])
                self.assertEqual(len(calls), 2)
                self.assertIsNone(result["decision"].file_document_id)

    def test_unknown_reason_text_is_not_logged_or_retried(self):
        _, result, calls, trace = self.run_guard([
            {"approved": False, "reason": "private customer detail"},
        ])
        self.assertFalse(result["grounding_approved"])
        self.assertEqual(len(calls), 1)
        self.assertNotIn("private customer detail", str(trace.call_args_list))

    def test_slow_turn_does_not_add_repair_calls(self):
        state = {**self.state(), "started_at": 1.0}
        with patch("apps.ai_engagement.graph.evidence.monotonic", return_value=20.0):
            with patch("apps.ai_engagement.graph.evidence.OpenAIProvider") as provider:
                provider.return_value.generate_text.return_value = SimpleNamespace(
                    text='{"approved":false,"reason":"language_mismatch"}')
                result = check_grounding(state)
        provider.return_value.generate_text.assert_called_once()
        self.assertFalse(result["grounding_approved"])

    def test_repair_uses_short_provider_timeout(self):
        with patch("apps.ai_engagement.graph.evidence.OpenAIProvider") as provider:
            provider.return_value.generate_text.side_effect = [SimpleNamespace(text=json.dumps(item)) for item in (
                {"approved": False, "reason": "language_mismatch"},
                {"message": "हम फ़ॉलो-अप को स्वचालित करते हैं।"},
                {"approved": True, "reason": "approved"},
            )]
            check_grounding(self.state())
        self.assertEqual(provider.call_args.kwargs, {"timeout_seconds": 5})

    def test_unknown_reply_with_available_facts_is_regenerated_even_if_verifier_approves(self):
        state = self.state()
        state["decision"] = replace(state["decision"], reason_code="UNKNOWN_INFORMATION",
                                    message="I do not have that information.", file_document_id=None)
        with patch("apps.ai_engagement.graph.evidence.OpenAIProvider") as provider:
            provider.return_value.generate_text.side_effect = [SimpleNamespace(text=json.dumps(item)) for item in (
                {"approved": True, "reason": "approved"},
                {"message": "हम फ़ॉलो-अप को स्वचालित करते हैं।"},
                {"approved": True, "reason": "approved"},
            )]
            result = check_grounding(state)
        self.assertEqual(provider.return_value.generate_text.call_count, 3)
        self.assertTrue(result["grounding_approved"])
        self.assertIn("फ़ॉलो-अप", result["decision"].message)

    def test_unsupported_language_only_claim_is_rebuilt_from_knowledge(self):
        state = self.state()
        state["decision"] = replace(state["decision"], file_document_id=None)
        state["context"].knowledge = [{"content": "Basic costs ₹2999 per month."}]
        with patch("apps.ai_engagement.graph.evidence.OpenAIProvider") as provider:
            provider.return_value.generate_text.side_effect = [SimpleNamespace(text=json.dumps(item)) for item in (
                {"approved": False, "reason": "unsupported_claim"},
                {"message": "Basic costs ₹2999 per month."},
                {"approved": True, "reason": "approved"},
            )]
            result = check_grounding(state)
        self.assertTrue(result["grounding_approved"])
        self.assertEqual(result["decision"].message, "Basic costs ₹2999 per month.")
        self.assertEqual(result["decision"].crm_actions, [])
