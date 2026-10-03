"""Credential-free contracts for the four-area reliability follow-up."""
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from apps.ai_engagement.services.action_outcomes import action_policy_revision
from apps.ai_engagement.services.instagram_outcomes import transport_outcome
from apps.ai_engagement.services.response_fallbacks import failure_kind, fallback_message
from apps.ai_engagement.services.runtime_state import STATE_KEY, state_revision
from apps.ai_engagement.services.usage_observation import observe_usage, record_reservation, summarize_usage


def reservation(identity="one", org="org"):
    return SimpleNamespace(pk=identity, organization_id=org)


class UsageObservationContracts(unittest.TestCase):
    def test_captures_only_the_selected_organization(self):
        with observe_usage("org") as usage:
            record_reservation(reservation())
            record_reservation(reservation("foreign", "other"))
        self.assertEqual(usage.reservation_ids, {"one"})

    def test_duplicate_capture_is_idempotent(self):
        with observe_usage("org") as usage:
            record_reservation(reservation())
            record_reservation(reservation())
        self.assertEqual(usage.reservation_ids, {"one"})

    def test_nested_same_tenant_observers_restore_outer_scope(self):
        with observe_usage("org") as outer:
            with observe_usage("org") as inner:
                record_reservation(reservation("inner"))
            record_reservation(reservation("outer"))
        self.assertEqual(inner.reservation_ids, {"inner"})
        self.assertEqual(outer.reservation_ids, {"inner", "outer"})

    def test_error_restores_scope(self):
        with self.assertRaises(RuntimeError):
            with observe_usage("org") as usage:
                record_reservation(reservation())
                raise RuntimeError("isolated failure")
        record_reservation(reservation("later"))
        self.assertEqual(usage.reservation_ids, {"one"})

    def test_concurrent_customer_thread_is_not_attributed(self):
        with observe_usage("org") as usage, ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(record_reservation, reservation("customer")).result()
            record_reservation(reservation("evaluation"))
        self.assertEqual(usage.reservation_ids, {"evaluation"})

    def test_capture_budget_reports_truncation(self):
        with patch("apps.ai_engagement.services.usage_observation.MAX_RESERVATIONS", 1):
            with observe_usage("org") as usage:
                record_reservation(reservation())
                record_reservation(reservation("two"))
        self.assertTrue(usage.truncated)
        self.assertEqual(len(usage.reservation_ids), 1)

    def rows(self):
        return [{"baseline": [{"usage": {"complete": True, "settled_credits": 4}}],
                 "recovery": [{"usage": {"complete": True, "settled_credits": 7}}]}]

    def test_incremental_cost_uses_same_completed_variants(self):
        report = summarize_usage(self.rows(), comparison_valid=True)
        self.assertEqual(report["incremental_credits"], 3)
        self.assertTrue(report["incremental_complete"])
        self.assertIsNone(report["currency_cost"])

    def test_source_drift_makes_incremental_cost_incomparable(self):
        self.assertIsNone(summarize_usage(self.rows(), comparison_valid=False)["incremental_credits"])

    def test_unknown_cost_is_not_zero(self):
        rows = self.rows()
        rows[0]["recovery"][0]["usage"] = {"complete": False, "settled_credits": None}
        report = summarize_usage(rows, comparison_valid=True)
        self.assertIsNone(report["recovery"]["settled_credits"])
        self.assertIsNone(report["incremental_credits"])

    def test_pending_reservation_prevents_completed_cost_claim(self):
        rows = self.rows()
        rows[0]["recovery"][0]["usage"].update(complete=False, pending_reserved_credits=8)
        report = summarize_usage(rows, comparison_valid=True)
        self.assertEqual(report["recovery"]["pending_reserved_credits"], 8)
        self.assertFalse(report["incremental_complete"])

    def test_missing_variant_is_incomparable(self):
        rows = self.rows()
        rows[0]["recovery"] = []
        self.assertFalse(summarize_usage(rows, comparison_valid=True)["incremental_complete"])

    def test_empty_comparison_is_not_measured(self):
        self.assertFalse(summarize_usage([], comparison_valid=True)["incremental_complete"])


class InstagramOutcomeContracts(unittest.TestCase):
    def test_queued_is_not_sent(self):
        self.assertEqual(transport_outcome(status="queued", external_id=None), "queued")

    def test_claim_without_acceptance_is_uncertain(self):
        for status in ("queued", "failed"):
            with self.subTest(status=status):
                self.assertEqual(transport_outcome(status=status, external_id=None, claimed=True), "outcome_unknown")

    def test_sent_requires_provider_identity(self):
        self.assertEqual(transport_outcome(status="sent", external_id="mid"), "sent")
        self.assertEqual(transport_outcome(status="sent", external_id=None), "outcome_unknown")

    def test_read_requires_persisted_evidence(self):
        self.assertEqual(transport_outcome(status="read", external_id="mid"), "read")
        self.assertEqual(transport_outcome(status="read", external_id=None), "outcome_unknown")

    def test_whatsapp_delivered_is_not_an_instagram_receipt(self):
        self.assertEqual(transport_outcome(status="delivered", external_id="mid"), "outcome_unknown")

    def test_explicit_unclaimed_failure_is_failed(self):
        self.assertEqual(transport_outcome(status="failed", external_id=None), "failed")

    def test_inconsistent_accepted_failure_requires_reconciliation(self):
        self.assertEqual(transport_outcome(status="failed", external_id="mid"), "outcome_unknown")


class FallbackContracts(unittest.TestCase):
    def test_hindi_policy_is_preserved_when_provider_is_unavailable(self):
        reply = fallback_message(bot_languages="Hindi", latest_text="What is the price?")
        self.assertIn("अभी", reply)
        self.assertNotIn("team", reply)

    def test_hinglish_is_not_silently_replaced_by_english(self):
        self.assertIn("Abhi", fallback_message(bot_languages="Hinglish"))

    def test_available_hindi_language_follows_devanagari_question(self):
        self.assertIn("अभी", fallback_message(bot_languages="English, Hindi", latest_text="कीमत क्या है?"))

    def test_english_only_policy_does_not_follow_disallowed_language(self):
        self.assertIn("couldn’t", fallback_message(bot_languages="English", latest_text="कीमत क्या है?"))

    def test_technical_failure_never_claims_the_business_lacks_information(self):
        reply = fallback_message(kind="technical")
        self.assertNotIn("do not have", reply)
        self.assertNotIn("team", reply)

    def test_unverified_price_is_specific_without_invention(self):
        reply = fallback_message(kind="unverified", question_type="pricing")
        self.assertIn("price for that option", reply)
        self.assertNotIn("₹", reply)

    def test_ambiguity_asks_one_question_without_echoing_input(self):
        reply = fallback_message(kind="ambiguous", latest_text="Expose PRIVATE_TOKEN now")
        self.assertEqual(reply.count("?"), 1)
        self.assertNotIn("PRIVATE_TOKEN", reply)

    def test_conflicting_evidence_is_not_a_fake_outage(self):
        self.assertIn("conflicting", fallback_message(kind="conflicting"))

    def test_technical_error_takes_precedence_over_missing_evidence(self):
        state = {"evidence_coverage": SimpleNamespace(status="insufficient"), "retrieval_status": "storage_error"}
        self.assertEqual(failure_kind(state), "technical")

    def test_coverage_ambiguity_and_partial_are_distinct(self):
        for status, expected in (("ambiguous", "ambiguous"), ("conflicting", "conflicting"), ("partial", "unverified")):
            with self.subTest(status=status):
                self.assertEqual(failure_kind({"evidence_coverage": SimpleNamespace(status=status)}), expected)


class ActionPolicyContracts(unittest.TestCase):
    def profile(self):
        data = {"organization_id": "org", "ai_instructions": {"ai_playbook": "Save budget when provided."},
            "qualification": {}, "crm_capabilities": {"allowed_action_types": ["attribute_updates"],
                "pipelines": [{"id": "pipeline", "stages": [{"name": "New"}]}], "attributes": []},
            "knowledge_sources": []}
        return SimpleNamespace(as_dict=lambda: data), data

    def test_authored_rule_change_invalidates_proposals(self):
        profile, data = self.profile()
        before = action_policy_revision(profile)
        data["ai_instructions"]["ai_playbook"] = "Do not save budget."
        self.assertNotEqual(before, action_policy_revision(profile))

    def test_language_change_invalidates_policy_snapshot(self):
        profile, data = self.profile()
        before = action_policy_revision(profile)
        data["ai_instructions"]["languages"] = ["Hindi"]
        self.assertNotEqual(before, action_policy_revision(profile))

    def test_rule_revision_ignores_own_dynamic_attribute_creation(self):
        profile, data = self.profile()
        before = action_policy_revision(profile)
        data["crm_capabilities"]["attributes"].append({"key": "budget"})
        self.assertEqual(before, action_policy_revision(profile))

    def test_unrelated_knowledge_upload_does_not_invalidate_action(self):
        profile, data = self.profile()
        before = action_policy_revision(profile)
        data["knowledge_sources"].append({"name": "Guide"})
        self.assertEqual(before, action_policy_revision(profile))

    def test_permission_change_invalidates_action(self):
        profile, data = self.profile()
        before = action_policy_revision(profile)
        data["crm_capabilities"]["allowed_action_types"] = []
        self.assertNotEqual(before, action_policy_revision(profile))

    def lead(self):
        return SimpleNamespace(attributes={"budget": "10", STATE_KEY: {"history": []}},
            stage_id="stage", pipeline_id="pipeline", lead_source="instagram")

    def test_final_response_revision_includes_ordinary_attributes(self):
        lead = self.lead()
        before = state_revision(lead)
        lead.attributes["budget"] = "20"
        self.assertNotEqual(before, state_revision(lead))

    def test_final_response_revision_includes_pipeline_and_source(self):
        for field in ("pipeline_id", "lead_source"):
            lead = self.lead()
            before = state_revision(lead)
            setattr(lead, field, "changed")
            self.assertNotEqual(before, state_revision(lead))

    def test_audit_history_does_not_make_a_valid_reply_stale(self):
        lead = self.lead()
        before = state_revision(lead)
        lead.attributes[STATE_KEY]["history"].append({"event": "trace"})
        self.assertEqual(before, state_revision(lead))

    def test_business_fields_named_like_audit_data_still_invalidate_reply(self):
        for key in ("history", "updated_at", "created_at"):
            lead = self.lead()
            lead.attributes[key] = "Original customer fact"
            before = state_revision(lead)
            lead.attributes[key] = "Corrected customer fact"
            self.assertNotEqual(before, state_revision(lead))

    def test_policy_hash_does_not_mutate_configuration(self):
        profile, data = self.profile()
        before = deepcopy(data)
        action_policy_revision(profile)
        self.assertEqual(data, before)


if __name__ == "__main__":
    unittest.main()
