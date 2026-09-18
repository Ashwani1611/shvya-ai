from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.ai_engagement.graph import evidence as graph
from apps.ai_engagement.services import phase5_6_runtime
from apps.ai_engagement.services.engagement import EngagementDecision
from apps.ai_engagement.services.evidence_resolver import (
    EvidenceItem, EvidenceResolution, GroundingCategory, InformationClass,
)
from apps.ai_engagement.services.grounding_safety import exact_evidence_reply
from apps.ai_engagement.services.phase7_completion_runtime import _deterministically_supported_reply


class GroundingSafetyContractTests(SimpleTestCase):
    def decision(self, message):
        return EngagementDecision(should_engage=True, message=message, file_document_id=None,
                                  crm_actions=[], qualification_updates=[], reason="ANSWER_ORG_QUESTION",
                                  reason_code="ANSWER_ORG_QUESTION", model="test")

    def resolution(self, text, kind="pricing"):
        return EvidenceResolution(category=GroundingCategory.STRUCTURED_ORG_DATA,
            information_class=InformationClass.STATIC_CONFIGURED, question_type=kind,
            sensitive=True, verified=True, evidence=(EvidenceItem(source_id="org:fact",
            source_type="organization_runtime_profile", content=text),))

    def run_guard(self, message, resolution):
        org, lead = SimpleNamespace(id="org-a"), SimpleNamespace(id="lead-a")
        token = phase5_6_runtime._ACTIVE_EVIDENCE.set({
            "organization_id": org.id, "lead_id": lead.id, "resolution": resolution})
        try:
            context = SimpleNamespace(organization={}, lead={"attributes": {}}, knowledge=[],
                                      conversation={"messages": []})
            state = {"organization": org, "lead": lead, "context": context,
                     "decision": self.decision(message), "requirements": [],
                     "qualification_state": {}, "runtime_policy": {}, "latest_text": "What is the price?"}
            with patch.object(graph, "OpenAIProvider") as provider:
                provider.return_value.generate_text.return_value = SimpleNamespace(text='{"approved": false}')
                result = graph.check_grounding(state)
                calls = provider.return_value.generate_text.call_count
            return result, calls
        finally:
            phase5_6_runtime._ACTIVE_EVIDENCE.reset(token)

    def test_hallucination_adversarial_replies_never_take_installed_fast_path(self):
        cases = [
            ("Price is 99 per month", "Price is 10 per month", "pricing"),
            ("Price is 1 per month", "Price is 2 per month", "pricing"),
            ("Price is ₹1.50 per month", "Price is ₹150 per month", "pricing"),
            ("Refunds are not available", "Refunds are available", "policy"),
            ("Refunds are available only within 7 days", "Refunds are available", "policy"),
            ("Pro price is 999 per month", "Pro price is 999 per month. Guaranteed discount.", "pricing"),
            ("₹999/month", "Our price is ₹999/month. You also get a refund guarantee.", "pricing"),
            ("Price is ₹99 per month", "Price is $99 per month", "pricing"),
            ("Open from 9 to 17", "Open from 17 to 9", "working_hours"),
            ("रिफंड उपलब्ध नहीं है", "रिफंड उपलब्ध है", "policy"),
        ]
        for fact, reply, kind in cases:
            with self.subTest(reply=reply):
                resolution = self.resolution(fact, kind)
                self.assertFalse(_deterministically_supported_reply(self.decision(reply), resolution))
                result, calls = self.run_guard(reply, resolution)
                self.assertFalse(result["grounding_approved"])
                self.assertEqual(calls, 1)
                self.assertNotEqual(result["decision"].message, reply)
                self.assertEqual(result["decision"].crm_actions, [])

    def test_exact_full_evidence_keeps_zero_verifier_call_budget(self):
        result, calls = self.run_guard("Refunds are not available", self.resolution("Refunds are not available", "policy"))
        self.assertTrue(result["grounding_approved"])
        self.assertEqual(calls, 0)

    def test_scalar_price_template_is_bounded_not_a_substring_allowlist(self):
        resolution = self.resolution("₹999/month")
        self.assertTrue(exact_evidence_reply(self.decision("Our price is ₹999/month."), resolution, allow_price_template=True))
        self.assertFalse(exact_evidence_reply(self.decision("Our price is ₹999/month. Free support forever."), resolution, allow_price_template=True))

    def test_fact_words_cannot_be_pooled_across_different_plans(self):
        resolution = self.resolution("Basic costs 99. Pro costs 999.")
        self.assertFalse(exact_evidence_reply(self.decision("Pro costs 99."), resolution))

    def test_model_reason_code_is_not_a_grounding_bypass(self):
        decision = self.decision("Our product supports every integration.")
        from dataclasses import replace
        from apps.ai_engagement.services.grounding_safety import safe_acknowledgement
        decision = replace(decision, reason_code="NORMAL_CONVERSATION")
        self.assertFalse(safe_acknowledgement(decision, None))
