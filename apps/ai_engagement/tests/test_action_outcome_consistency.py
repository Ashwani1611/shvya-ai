"""Live-source action receipts and authored-policy changes at execution."""
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.ai_engagement.models import AIActionReceipt, OrgInfo
from apps.ai_engagement.services.action_planner import ActionPlanner
from apps.ai_engagement.services.context import AIContextBuilder
from apps.ai_engagement.services.crm_executor import CRMActionExecutor, CRMActionExecutionError
from apps.ai_engagement.services.transactional_decision_reuse import operational_state_for_context
from apps.ai_engagement.services.runtime_state import STATE_KEY
from apps.ai_engagement.tests import test_engagement_controls as fixtures
from apps.crm.models import AttributeDefinition, LeadReminder


class ActionOutcomeConsistencyTests(TestCase):
    setUp = fixtures.AIEngagementControlTests.setUp
    _inbound = fixtures.AIEngagementControlTests._inbound

    def context(self):
        self.lead.refresh_from_db()
        return AIContextBuilder().build(organization=self.organization, lead=self.lead)

    def reminder_action(self):
        return {"type": "create_reminder", "title": "Follow up", "description": "Private operational description",
                "due_at": (timezone.now() + timedelta(days=1)).isoformat()}

    def test_receipt_is_the_authority_even_without_runtime_marker(self):
        source = self._inbound()
        CRMActionExecutor().execute(organization=self.organization, lead=self.lead,
            actions=[self.reminder_action()], source_message=source)
        resolved = operational_state_for_context(self.context())["resolved_actions"]
        self.assertEqual(resolved["source_message_id"], str(source.pk))
        self.assertEqual(resolved["action_types"], ["create_reminder"])
        self.assertEqual(resolved["outcomes"][0]["status"], "executed")
        self.assertTrue(resolved["outcomes"][0]["still_exists"])
        self.assertNotIn("Private operational description", json.dumps(resolved))

    def test_historical_reminder_receipt_does_not_claim_it_is_still_active(self):
        source = self._inbound()
        CRMActionExecutor().execute(organization=self.organization, lead=self.lead,
            actions=[self.reminder_action()], source_message=source)
        LeadReminder.objects.filter(lead=self.lead).delete()
        outcome = operational_state_for_context(self.context())["resolved_actions"]["outcomes"][0]
        self.assertEqual(outcome["status"], "executed")
        self.assertFalse(outcome["still_exists"])
        self.assertEqual(outcome["current_status"], "absent")

    def test_unreceipted_marker_cannot_claim_execution(self):
        source = self._inbound()
        self.lead.attributes = {STATE_KEY: {"pre_resolved_message_id": str(source.pk),
                                          "pre_resolved_actions": ["create_reminder"]}}
        self.lead.save(update_fields=["attributes"])
        resolved = operational_state_for_context(self.context())["resolved_actions"]
        self.assertEqual(resolved["action_types"], [])
        self.assertEqual(resolved["outcomes"], [])

    def test_prior_source_receipt_cannot_support_a_new_confirmation(self):
        source = self._inbound()
        CRMActionExecutor().execute(organization=self.organization, lead=self.lead,
            actions=[self.reminder_action()], source_message=source)
        self._inbound("new-turn")
        resolved = operational_state_for_context(self.context())["resolved_actions"]
        self.assertEqual(resolved["outcomes"], [])
        self.assertEqual(resolved["action_types"], [])

    def test_attribute_no_op_is_persisted_but_not_claimed_as_new_execution(self):
        AttributeDefinition.objects.create(organization=self.organization, name="Industry", key="industry")
        self.lead.attributes = {"industry": "Retail"}
        self.lead.save(update_fields=["attributes"])
        source = self._inbound()
        source.body = "My industry is Retail"
        source.save(update_fields=["body"])
        result = CRMActionExecutor().execute(organization=self.organization, lead=self.lead, source_message=source,
            actions=[{"type": "attribute_updates", "updates": [{"key": "industry", "value": "Retail"}]}])
        self.assertEqual(result[0]["status"], "no_op")
        self.assertEqual(result[0]["changed_keys"], [])
        resolved = operational_state_for_context(self.context())["resolved_actions"]
        self.assertEqual(resolved["action_types"], [])
        self.assertEqual(resolved["outcomes"][0]["status"], "no_op")

    def test_playbook_change_after_planning_blocks_new_mutations(self):
        source = self._inbound()
        info, _ = OrgInfo.objects.get_or_create(organization=self.organization)
        info.ai_playbook = "## Rules\nUse English."
        info.save()
        plan = ActionPlanner().plan(organization=self.organization, lead=self.lead,
            decision=SimpleNamespace(crm_actions=[self.reminder_action()], file_document_id=None, reason_code=""),
            source_message=source)
        self.assertTrue(plan.accepted_actions)
        info.ai_playbook = "## Rules\nUse Hindi. Do not promise callbacks."
        info.save()
        with patch.object(ActionPlanner, "plan", return_value=plan):
            with self.assertRaisesRegex(CRMActionExecutionError, "policy changed"):
                CRMActionExecutor().execute(organization=self.organization, lead=self.lead,
                    actions=[self.reminder_action()], source_message=source)
        self.assertFalse(LeadReminder.objects.filter(lead=self.lead).exists())
        self.assertFalse(AIActionReceipt.objects.filter(lead=self.lead).exists())

    def test_receipt_replay_remains_idempotent(self):
        source = self._inbound()
        action = self.reminder_action()
        executor = CRMActionExecutor()
        first = executor.execute(organization=self.organization, lead=self.lead, actions=[action], source_message=source)
        second = executor.execute(organization=self.organization, lead=self.lead, actions=[action], source_message=source)
        self.assertEqual(first[0]["reminder_id"], second[0]["reminder_id"])
        self.assertTrue(second[0]["idempotent_replay"])
        self.assertEqual(AIActionReceipt.objects.filter(lead=self.lead).count(), 1)

    def test_malformed_receipt_does_not_add_free_text_to_generation(self):
        source = self._inbound()
        AIActionReceipt.objects.create(organization=self.organization, lead=self.lead, source_message_id=source.pk,
            idempotency_key="malformed", action_type="CREATE_REMINDER", result={"type": "create_reminder",
                "status": "PRIVATE_SECRET_TEXT", "description": "PRIVATE_SECRET_TEXT", "reminder_id": "invalid"})
        resolved = operational_state_for_context(self.context())["resolved_actions"]
        self.assertNotIn("PRIVATE_SECRET_TEXT", json.dumps(resolved))
        self.assertEqual(resolved["outcomes"][0]["status"], "unknown")
        self.assertEqual(resolved["action_types"], [])

    def test_malformed_list_fields_are_omitted(self):
        source = self._inbound()
        for name, kind, fields in (
            ("UPDATE_ATTRIBUTE", "attribute_updates", {"keys": 42}),
            ("UPDATE_CONTACT", "contact_updates", {"contact_ids": True}),
        ):
            AIActionReceipt.objects.create(organization=self.organization, lead=self.lead, source_message_id=source.pk,
                idempotency_key=name, action_type=name, result={"type": kind, "status": "executed", **fields})
        resolved = operational_state_for_context(self.context())["resolved_actions"]
        self.assertEqual(len(resolved["outcomes"]), 2)
        self.assertEqual(resolved["outcomes"][0].get("keys", []), [])

    def test_malformed_status_values_cannot_break_final_composition(self):
        source = self._inbound()
        for index, value in enumerate((["executed"], {"status": "executed"}, None, True)):
            AIActionReceipt.objects.create(organization=self.organization, lead=self.lead,
                source_message_id=source.pk, idempotency_key="bad-status-" + str(index),
                action_type="CREATE_REMINDER", result={"type": "create_reminder", "status": value})
        resolved = operational_state_for_context(self.context())["resolved_actions"]
        self.assertEqual(len(resolved["outcomes"]), 4)
        self.assertEqual(resolved["action_types"], [])
        self.assertTrue(all(row["status"] == "unknown" for row in resolved["outcomes"]))
