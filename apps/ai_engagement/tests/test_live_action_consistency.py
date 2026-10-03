"""Persisted action receipts, source rules and final language-only regressions."""
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from apps.ai_engagement.models import AIActionReceipt, OrgInfo
from apps.ai_engagement.services.crm_executor import CRMActionExecutor, CRMActionExecutionError
from apps.ai_engagement.services.engagement import EngagementDecision
from apps.ai_engagement.services.turn_action_consistency import (
    assert_policy_current, live_operational_state, policy_fingerprint,
)
from apps.ai_engagement.services.playbook_scope import rule_applies
from apps.ai_engagement.tests import test_engagement_controls as fixtures
from apps.ai_engagement.tests.test_grounding_reply_repair import GroundingReplyRepairTests
from apps.crm.models import AttributeDefinition, LeadNote


class LiveActionConsistencyTests(TestCase):
    setUp = fixtures.AIEngagementControlTests.setUp
    _inbound = fixtures.AIEngagementControlTests._inbound

    def context(self, source):
        return SimpleNamespace(organization={"id": str(self.organization.pk)}, lead={"id": str(self.lead.pk)},
            conversation={"channel": "whatsapp", "messages": [{"id": str(source.pk), "direction": "inbound"}]})

    def test_proposal_markers_do_not_prove_completed_actions(self):
        from apps.ai_engagement.services.runtime_state import STATE_KEY
        source = self._inbound("markers-only")
        self.lead.attributes = {STATE_KEY: {"pre_resolved_message_id": str(source.pk),
                                         "pre_resolved_actions": ["create_reminder", "attribute_updates"]}}
        self.lead.save(update_fields=["attributes"])
        state = live_operational_state(lead=self.lead, context=self.context(source))
        self.assertEqual(state["resolved_actions"]["action_types"], [])
        self.assertEqual(state["resolved_actions"]["outcomes"], [])

    def test_actual_execution_receipt_is_visible_only_for_its_source(self):
        a, b = self._inbound("receipt-a"), self._inbound("receipt-b")
        CRMActionExecutor().execute(organization=self.organization, lead=self.lead, source_message=a,
            actions=[{"type": "add_note", "note": "Approved test note"}])
        self.assertIn("add_note", live_operational_state(lead=self.lead, context=self.context(a))["resolved_actions"]["action_types"])
        self.assertEqual(live_operational_state(lead=self.lead, context=self.context(b))["resolved_actions"]["action_types"], [])
        self.assertNotIn("Approved test note", json.dumps(live_operational_state(lead=self.lead, context=self.context(a))))

    def test_changed_playbook_rejects_generated_action_before_writes(self):
        info, _ = OrgInfo.objects.get_or_create(organization=self.organization)
        decision = EngagementDecision(should_engage=True, message="Test", file_document_id=None,
            crm_actions=[], reason="NORMAL_CONVERSATION", model="test", policy_revision=policy_fingerprint({
                "ai_playbook": info.ai_playbook, "bot_languages": info.bot_languages, "ai_enabled": info.ai_enabled}))
        assert_policy_current(organization=self.organization, decision=decision)
        OrgInfo.objects.filter(pk=info.pk).update(ai_playbook=info.ai_playbook + "\nNew authored restriction")
        before = (LeadNote.objects.count(), AIActionReceipt.objects.count())
        with self.assertRaises(CRMActionExecutionError):
            assert_policy_current(organization=self.organization, decision=decision)
        self.assertEqual(before, (LeadNote.objects.count(), AIActionReceipt.objects.count()))

    def test_source_scopes_are_not_current_channel_scopes(self):
        for source, expected in (("instagram", True), ("whatsapp", False), (None, False)):
            self.assertEqual(rule_applies("For Instagram leads, map Budget from Q1.",
                                         lead_source=source, channel="whatsapp"), expected)
        self.assertFalse(rule_applies("When lead source is Instagram or WhatsApp, map Budget.", lead_source="instagram"))

    def test_qualification_mapping_keeps_its_authored_source_condition(self):
        from apps.ai_engagement.services.qualification_execution.config import _config, _mapping_keys
        from apps.ai_engagement.services.organization_profile import compile_qualification_requirements
        info, _ = OrgInfo.objects.get_or_create(organization=self.organization)
        AttributeDefinition.objects.get_or_create(organization=self.organization, key="budget", defaults={"name": "Budget"})
        questions = "What is your budget?"
        info.ai_playbook = ("## Qualification Questions\n" + questions + "\n## Attribute mapping logic\n"
            "Mapping 1:\n- Attribute name: Budget\n- Source: Q1\n- Condition: lead source is Instagram.\n")
        info.save()
        requirements = compile_qualification_requirements(questions)["requirements"]
        config = _config(organization=self.organization, requirements=requirements)
        self.assertIn("budget", _mapping_keys(config, requirements[0]["id"], lead_source="instagram", channel="whatsapp"))
        self.assertNotIn("budget", _mapping_keys(config, requirements[0]["id"], lead_source="whatsapp", channel="instagram"))

    def test_optional_attribute_creation_failure_is_not_an_executed_action(self):
        from django.core.exceptions import ValidationError
        with patch("services.crm.attribute_service.create_attribute_definition", side_effect=ValidationError("capacity")):
            result = CRMActionExecutor()._execute_attribute_updates(organization=self.organization, lead=self.lead,
                action={"type": "attribute_updates", "updates": [{"key": "unique_capacity_test", "name": "Unique capacity test",
                         "value": "Retail", "create_if_missing": True, "field_type": "text"}]})
        self.assertEqual(result["status"], "not_applied")
        self.assertEqual(result["keys"], [])
        self.assertEqual(result["skipped_keys"], ["unique_capacity_test"])


class FinalCompositionChecks(SimpleTestCase):
    state = GroundingReplyRepairTests.state

    def test_post_action_language_failure_can_be_corrected_once_and_revalidated(self):
        from apps.ai_engagement.graph.evidence import check_grounding
        from apps.ai_engagement.services.post_state_finalization_guard import _FINAL_LANGUAGE_ONLY
        token = _FINAL_LANGUAGE_ONLY.set(True)
        try:
            with patch("apps.ai_engagement.graph.evidence.OpenAIProvider") as provider:
                provider.return_value.generate_text.side_effect = [SimpleNamespace(text=json.dumps(item)) for item in (
                    {"approved": False, "reason": "language_mismatch"},
                    {"message": "हम फ़ॉलो-अप को स्वचालित करते हैं।"}, {"approved": True, "reason": "approved"},
                )]
                result = check_grounding(self.state())
                self.assertEqual(provider.return_value.generate_text.call_count, 3)
            self.assertTrue(result["grounding_approved"])
            self.assertEqual(result["decision"].crm_actions, [])
        finally:
            _FINAL_LANGUAGE_ONLY.reset(token)

    def test_final_validation_failure_cannot_revive_the_selected_file(self):
        from apps.ai_engagement.services.canonical_architecture import ResponseActionValidator
        decision = replace(self.state()["decision"], message="The file has been sent.", final_validation_failed=True)
        result = ResponseActionValidator().validate(decision=decision, reconciled_state={
            "file_share": {"document_id": 7, "status": "resolved_pending_send"}, "response_language": "hindi"})
        self.assertIsNone(result.file_document_id)
        self.assertNotIn("has been sent", result.message)

    def test_technical_failure_in_final_gate_uses_allowed_language(self):
        from apps.ai_engagement.graph.evidence import check_grounding
        from apps.ai_engagement.services.ai_provider import AIProviderPermanentError
        with patch("apps.ai_engagement.graph.evidence.OpenAIProvider") as provider:
            provider.return_value.generate_text.side_effect = AIProviderPermanentError("private error")
            result = check_grounding(self.state())
        self.assertFalse(result["grounding_approved"])
        self.assertTrue(result["decision"].final_validation_failed)
        self.assertIn("तकनीकी", result["decision"].message)
        self.assertNotIn("private error", result["decision"].message)

    def test_available_but_unrelated_context_does_not_force_a_recovery_loop(self):
        from apps.ai_engagement.graph.evidence import check_grounding
        state = self.state()
        state["evidence_coverage"] = SimpleNamespace(status="insufficient")
        state["decision"] = replace(state["decision"], file_document_id=None, reason_code="UNKNOWN_INFORMATION",
                                    message="इस विशेष जानकारी का सत्यापित उत्तर अभी उपलब्ध नहीं है।")
        with patch("apps.ai_engagement.graph.evidence.OpenAIProvider") as provider:
            provider.return_value.generate_text.return_value = SimpleNamespace(text='{"approved":true,"reason":"approved"}')
            result = check_grounding(state)
            provider.return_value.generate_text.assert_called_once()
        self.assertTrue(result["grounding_approved"])
