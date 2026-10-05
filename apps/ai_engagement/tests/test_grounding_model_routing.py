"""Conversation verification and repairs honor superadmin's tenant model choice."""
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.ai_engagement.graph.evidence import check_grounding
from apps.ai_engagement.services.evidence_recovery import _assess
from apps.ai_engagement.services.turn_controller import build_turn_policy
from apps.ai_engagement.tests import test_grounding_verdict_contract as fixtures


class GroundingModelRoutingTests(SimpleTestCase):
    def state(self, stage="New Lead", *, saved_policy=False, org_id="org", prefix="tenant"):
        state = fixtures.GroundingVerdictContractTests().state()
        state["organization"].id = org_id
        state["context"].organization.update(
            id=org_id, qualification_model=f"{prefix}-qualification",
            sales_support_model=f"{prefix}-sales", summary_model=f"{prefix}-summary",
        )
        state["context"].stage = {"name": stage}
        # Real stage must win even if the qualification snapshot is stale.
        state["qualification_state"] = {"engagement_mode": "qualification"}
        if saved_policy:
            state["turn_policy"] = build_turn_policy(
                context=state["context"], qualification_state=state["qualification_state"],
            )
        return state

    def test_initial_check_and_all_bounded_repairs_use_the_turn_model(self):
        for stage, expected in (("New Lead", "tenant-qualification"),
                                ("New leads", "tenant-qualification"), ("Qualified", "tenant-sales")):
            for saved_policy in (False, True):
                with self.subTest(stage=stage, saved_policy=saved_policy):
                    state = self.state(stage, saved_policy=saved_policy)
                    with patch("apps.ai_engagement.graph.evidence.OpenAIProvider") as provider:
                        provider.return_value.generate_text.side_effect = [
                            SimpleNamespace(text=json.dumps(payload)) for payload in (
                                {"approved": "true", "reason": "approved"},
                                {"approved": False, "reason": "language_mismatch"},
                                {"message": fixtures.PRICING_REPLY},
                                {"approved": True, "reason": "approved"},
                            )
                        ]
                        result = check_grounding(state)
                    self.assertTrue(result["grounding_approved"])
                    calls = provider.return_value.generate_text.call_args_list
                    self.assertEqual(len(calls), 4)
                    self.assertEqual([call.kwargs["metadata"]["phase"] for call in calls],
                                     ["grounding", "grounding_contract_retry", "grounding_reply_repair", "grounding"])
                    self.assertTrue(all(call.kwargs["metadata"].get("model_override") == expected for call in calls))
                    self.assertTrue(all(call.kwargs["metadata"]["organization_id"] == "org" for call in calls))

    def test_consecutive_organizations_do_not_share_model_routing(self):
        with patch("apps.ai_engagement.graph.evidence.OpenAIProvider") as provider:
            provider.return_value.generate_text.return_value.text = '{"approved":true,"reason":"approved"}'
            for org in ("org-a", "org-b"):
                self.assertTrue(check_grounding(self.state(org_id=org, prefix=org))["grounding_approved"])
        self.assertEqual([
            (call.kwargs["metadata"]["organization_id"], call.kwargs["metadata"].get("model_override"))
            for call in provider.return_value.generate_text.call_args_list
        ], [("org-a", "org-a-qualification"), ("org-b", "org-b-qualification")])

    def test_blank_override_keeps_the_platform_default_and_one_check(self):
        state = self.state()
        state["context"].organization["qualification_model"] = ""
        with patch("apps.ai_engagement.graph.evidence.OpenAIProvider") as provider:
            provider.return_value.generate_text.return_value.text = '{"approved":true,"reason":"approved"}'
            self.assertTrue(check_grounding(state)["grounding_approved"])
        provider.return_value.generate_text.assert_called_once()
        self.assertEqual(provider.return_value.generate_text.call_args.kwargs["metadata"].get("model_override"), "")

    def test_optional_evidence_coverage_uses_the_same_stage_model(self):
        for stage, expected in (("New Lead", "tenant-qualification"), ("Qualified", "tenant-sales")):
            with self.subTest(stage=stage):
                state = self.state(stage)
                with patch("apps.ai_engagement.services.evidence_recovery.remaining", return_value=20), \
                     patch("apps.ai_engagement.services.ai_provider.OpenAIProvider") as provider:
                    provider.return_value.generate_text.return_value.text = json.dumps({
                        "status": "sufficient", "parts": [{"question": "What is the price?",
                            "supported": True, "source_ids": ["faq:price"]}], "retry_query": "",
                    })
                    result = _assess(state, [{"source_id": "faq:price", "content": fixtures.PRICING_FACT}])
                self.assertEqual(result.status, "sufficient")
                provider.return_value.generate_text.assert_called_once()
                metadata = provider.return_value.generate_text.call_args.kwargs["metadata"]
                self.assertEqual(metadata.get("model_override"), expected)
                self.assertEqual(metadata["phase"], "evidence_coverage")
