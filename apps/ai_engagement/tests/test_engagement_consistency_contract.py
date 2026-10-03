"""Credential-free regressions for source-bound outcomes and honest fallback/costs."""
import unittest
from types import SimpleNamespace
from uuid import uuid4

from apps.ai_engagement.services.turn_action_consistency import (
    applied_action_types, policy_fingerprint, safe_action_results,
)
from apps.ai_engagement.services.playbook_scope import rule_applies, scoped_runtime_policy
from apps.ai_engagement.services.response_fallbacks import failure_text, grounding_failure_text, response_language
from apps.ai_engagement.services.instagram_delivery_outcomes import transport_status
from apps.ai_engagement.services.usage_attribution import (
    capture_reservation, capture_usage, summarize_rows, _SCOPES,
)


class ActionOutcomeContracts(unittest.TestCase):
    def test_proposals_and_noops_are_not_executed_actions(self):
        self.assertEqual(applied_action_types([
            {"type": "create_reminder"}, {"type": "pipeline_transition", "status": "no_op"},
            {"type": "attribute_updates", "status": "executed", "keys": []},
        ]), [])

    def test_partial_attribute_result_records_only_a_real_effect(self):
        self.assertEqual(applied_action_types([
            {"type": "attribute_updates", "status": "partial", "keys": ["industry"]},
            {"type": "create_reminder", "status": "executed"},
            {"type": "create_reminder", "status": "executed"},
        ]), ["attribute_updates", "create_reminder"])

    def test_malformed_legacy_receipts_do_not_crash_or_authorize(self):
        rows = [None, [], {"type": []}, {"type": "add_note", "status": []},
                {"type": "attribute_updates", "status": "executed", "keys": []}]
        self.assertEqual(applied_action_types(rows), [])
        self.assertEqual([item["status"] for item in safe_action_results(rows)], ["not_applied", "not_applied"])

    def test_receipt_projection_omits_contact_values_note_text_and_secrets(self):
        row = safe_action_results([{"type": "contact_updates", "status": "executed",
            "handle": "private", "note": "private", "value": "private", "access_token": "private"}])[0]
        self.assertEqual(set(row), {"type", "status", "idempotent_replay"})

    def test_policy_hash_is_stable_and_detects_each_authored_control(self):
        context = {"ai_playbook": "Use approved facts", "bot_languages": "Hindi", "ai_enabled": True}
        value = policy_fingerprint(context)
        self.assertEqual(value, policy_fingerprint({**context, "name": "ignored label", "_file_candidates": []}))
        for key, replacement in (("ai_playbook", "New rules"), ("bot_languages", "English"), ("ai_enabled", False)):
            self.assertNotEqual(value, policy_fingerprint({**context, key: replacement}))

    def test_channel_rules_do_not_infer_channel_from_lead_origin(self):
        rule = "When channel is Instagram, map Budget from Q1."
        self.assertTrue(rule_applies(rule, channel="instagram", lead_source="organic"))
        self.assertFalse(rule_applies(rule, channel="whatsapp", lead_source="instagram"))

    def test_ambiguous_channel_rules_fail_closed(self):
        for rule in ("When channel is Instagram or WhatsApp, send.", "When channel is not Instagram, send.",
                     "When channel is telegram, send."):
            self.assertFalse(rule_applies(rule, channel="instagram"))

    def test_runtime_rule_filter_is_read_only(self):
        policy = {"crm": {"reminders": ["When channel is Instagram, create reminder.", "Create reminder on request."]}}
        result = scoped_runtime_policy(policy, SimpleNamespace(lead={"lead_source": "instagram"}, conversation={"channel": "whatsapp"}))
        self.assertEqual(len(policy["crm"]["reminders"]), 2)
        self.assertEqual(len(result["crm"]["reminders"]), 1)
        self.assertFalse(result["applicability"]["authorizes_actions"])


class InstagramStatusContracts(unittest.TestCase):
    def message(self, status, external_id=None, **raw):
        return SimpleNamespace(status=status, external_id=external_id, raw_payload=raw)

    def test_queued_is_not_sent(self):
        self.assertEqual(transport_status(self.message("queued")), "queued")

    def test_sent_requires_provider_identity(self):
        self.assertEqual(transport_status(self.message("sent")), "delivery_unknown")
        self.assertEqual(transport_status(self.message("sent", "mid")), "sent")

    def test_read_requires_provider_identity_and_does_not_invent_delivered(self):
        self.assertEqual(transport_status(self.message("read", "mid")), "read")
        self.assertEqual(transport_status(self.message("read")), "delivery_unknown")

    def test_started_without_acceptance_remains_uncertain(self):
        self.assertEqual(transport_status(self.message("queued", shvya_send_started_at="started")), "delivery_unknown")
        self.assertEqual(transport_status(self.message("failed", shvya_send_outcome="delivery_unknown")), "delivery_unknown")

    def test_explicit_failed_result_stays_failed(self):
        self.assertEqual(transport_status(self.message("failed", shvya_send_outcome="rejected")), "failed")


class FallbackContracts(unittest.TestCase):
    def test_hindi_configuration_never_gets_english_failure(self):
        for kind in ("technical", "unverified", "conflict", "reminder", "file_failed", "file_unknown"):
            text = failure_text(kind, organization_context={"bot_languages": "Hindi"}, latest_text="What price?")
            self.assertTrue(any("\u0900" <= letter <= "\u097f" for letter in text))
            self.assertNotIn("I've noted", text)

    def test_unsupported_customer_language_cannot_override_allowed_languages(self):
        self.assertEqual(response_language({"bot_languages": "English"}, "क्या कीमत है?"), "english")
        self.assertEqual(response_language({"bot_languages": "English, Hindi"}, "क्या कीमत है?"), "hindi")

    def test_selected_and_queued_file_copy_are_distinct(self):
        self.assertNotIn("queued", failure_text("file_pending"))
        self.assertIn("queued", failure_text("file_queued"))
        self.assertNotIn("delivered", failure_text("file_unknown"))

    def test_failed_actions_do_not_claim_they_were_noted_or_scheduled(self):
        for kind in ("reminder", "booking", "handoff", "file_failed"):
            text = failure_text(kind)
            self.assertNotIn("I've noted", text)
            self.assertNotIn("I'm sending", text)

    def test_conflicting_evidence_and_technical_failure_have_different_wording(self):
        state = {"context": SimpleNamespace(organization={}), "evidence_coverage": SimpleNamespace(status="conflicting")}
        self.assertIn("conflict", grounding_failure_text(state, reason="missing_evidence"))
        self.assertIn("retrieve", grounding_failure_text(state, reason="provider_error"))


class CostAttributionContracts(unittest.TestCase):
    def rows(self):
        reservation = {"id": "r", "status": "settled", "actual_credits": 3, "reserved_credits": 5,
                       "actual_input_tokens": 100, "actual_output_tokens": 20}
        ledger = {"reservation_id": "r", "amount": -3, "input_tokens": 100, "output_tokens": 20}
        return reservation, ledger

    def test_context_capture_is_nested_and_tenant_scoped(self):
        org, foreign = uuid4(), uuid4()
        with capture_usage(org) as outer:
            with capture_usage(foreign) as other:
                with capture_usage(org) as inner:
                    reservation = SimpleNamespace(organization_id=org, pk=uuid4())
                    capture_reservation(reservation)
                    self.assertEqual(inner.reservation_ids, {str(reservation.pk)})
                self.assertEqual(other.reservation_ids, set())
            self.assertEqual(outer.reservation_ids, {str(reservation.pk)})
        self.assertEqual(_SCOPES.get(), ())

    def test_scope_restores_after_failure(self):
        with self.assertRaises(RuntimeError), capture_usage(uuid4()):
            raise RuntimeError("failed turn")
        self.assertEqual(_SCOPES.get(), ())

    def test_settled_credits_are_confirmed_by_ledger(self):
        row, ledger = self.rows()
        result = summarize_rows([row], [ledger], expected_count=1)
        self.assertTrue(result["accounting_complete"])
        self.assertEqual(result["total_credits"], 3)
        self.assertIsNone(result["currency_cost"])

    def test_pending_charge_is_not_reported_as_free(self):
        row, _ = self.rows(); row["status"] = "active"
        result = summarize_rows([row], [], expected_count=1)
        self.assertFalse(result["accounting_complete"])
        self.assertIsNone(result["total_credits"])
        self.assertEqual(result["pending_reserved_credits"], 5)

    def test_missing_or_duplicate_ledger_is_not_a_verified_cost(self):
        row, ledger = self.rows()
        for transactions in ([], [ledger, ledger]):
            result = summarize_rows([row], transactions, expected_count=1)
            self.assertFalse(result["accounting_complete"])
            self.assertIsNone(result["total_credits"])

    def test_failed_provider_released_reservation_costs_zero(self):
        row, _ = self.rows(); row["status"] = "released"
        self.assertEqual(summarize_rows([row], [], expected_count=1)["total_credits"], 0)

    def test_missing_or_truncated_reservations_report_incomplete(self):
        for count, truncated in ((1, False), (0, True)):
            self.assertFalse(summarize_rows([], [], expected_count=count, truncated=truncated)["accounting_complete"])

    def test_no_provider_calls_is_a_valid_zero_cost(self):
        self.assertEqual(summarize_rows([], [], expected_count=0)["total_credits"], 0)


if __name__ == "__main__":
    unittest.main()
