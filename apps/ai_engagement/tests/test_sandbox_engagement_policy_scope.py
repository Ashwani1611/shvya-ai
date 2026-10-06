"""Exercise the installed engagement wrapper, not just policy construction."""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.ai_engagement.services import conversation_policy_runtime as runtime
from apps.ai_engagement.services import engagement
from apps.ai_engagement.services.conversation_policy import (
    ConversationPolicyDecision, ConversationPolicyOutcome,
)


class SandboxEngagementPolicyScopeTests(SimpleTestCase):
    def setUp(self):
        self.organization = SimpleNamespace(pk="org", id="org")
        self.lead = SimpleNamespace(pk="playground:test", id="playground:test")
        self.policy = ConversationPolicyDecision(
            outcome=ConversationPolicyOutcome.ASK_QUALIFICATION,
            reason_code="NEXT_REQUIREMENT", confidence=1.0,
            continue_qualification=True, next_requirement_id="age",
        )
        self.turn_token = runtime._TURN.set(None)
        self.policy_token = runtime._POLICY.set(self.policy)
        self.scope_token = runtime._SANDBOX_POLICY_SCOPE.set({
            "organization": self.organization, "lead": self.lead,
            "policy": self.policy,
        })
        self.addCleanup(runtime._TURN.reset, self.turn_token)
        self.addCleanup(runtime._POLICY.reset, self.policy_token)
        self.addCleanup(runtime._SANDBOX_POLICY_SCOPE.reset, self.scope_token)

        class Service:
            def engage(self, **kwargs):
                return runtime._POLICY.get()

            def _build_input(self, **kwargs):
                return "{}"

            def _build_instructions(self, **kwargs):
                return ""

            def _validate_qualification_decision(self, **kwargs):
                return None

            def _should_retrieve_knowledge(self, **kwargs):
                return False

        with patch.object(engagement, "EngagementService", Service):
            runtime._patch_engagement()
        self.service = Service()

    def test_installed_wrapper_preserves_exact_sandbox_policy(self):
        with patch.object(runtime, "_policy_for_turn") as rebuild:
            result = self.service.engage(
                organization=self.organization, lead=self.lead,
            )
        self.assertIs(result, self.policy)
        rebuild.assert_not_called()

    def test_other_organization_cannot_reuse_policy(self):
        with patch.object(runtime, "_policy_for_turn", return_value=None) as rebuild:
            result = self.service.engage(
                organization=SimpleNamespace(pk="other", id="other"),
                lead=self.lead,
            )
        self.assertIsNone(result)
        rebuild.assert_called_once()

    def test_other_lead_object_cannot_reuse_policy_even_with_same_id(self):
        with patch.object(runtime, "_policy_for_turn", return_value=None) as rebuild:
            result = self.service.engage(
                organization=self.organization,
                lead=SimpleNamespace(pk=self.lead.pk, id=self.lead.id),
            )
        self.assertIsNone(result)
        rebuild.assert_called_once()

    def test_replaced_policy_cannot_use_old_scope(self):
        runtime._POLICY.set(ConversationPolicyDecision(
            outcome=ConversationPolicyOutcome.NORMAL_CONVERSATION,
            reason_code="STALE", confidence=1.0,
        ))
        with patch.object(runtime, "_policy_for_turn", return_value=None):
            result = self.service.engage(
                organization=self.organization, lead=self.lead,
            )
        self.assertIsNone(result)

    def test_unscoped_production_turn_rebuilds_policy(self):
        runtime._SANDBOX_POLICY_SCOPE.set(None)

        def rebuild(**kwargs):
            runtime._POLICY.set(self.policy)
            return self.policy

        with patch.object(runtime, "_policy_for_turn", side_effect=rebuild) as mocked:
            result = self.service.engage(
                organization=self.organization, lead=self.lead,
            )
        self.assertIs(result, self.policy)
        mocked.assert_called_once()
