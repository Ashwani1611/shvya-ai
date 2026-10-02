"""Credential-free contracts; runnable with unittest without Django setup."""
import json
import os
import unittest
from time import monotonic
from types import SimpleNamespace
from unittest.mock import patch

from apps.ai_engagement.services.evidence_recovery import (
    Coverage, enabled, parse_coverage, recovery_route, remaining,
)


FLAGS = {"AI_BRAIN_RECOVERY_ENABLED": "1", "AI_BRAIN_RECOVERY_ORGANIZATION_IDS": "org-a",
         "AI_BRAIN_RECOVERY_BUDGET_SECONDS": "20"}


def part(question="What is the price?", supported=True, ids=None):
    return {"question": question, "supported": supported, "source_ids": ["faq:1"] if ids is None else ids}


def verdict(status="sufficient", parts=None, query=""):
    return json.dumps({"status": status, "parts": [part()] if parts is None else parts, "retry_query": query})


class EvidenceCoverageContracts(unittest.TestCase):
    def test_supported_answer_requires_known_source(self):
        value = parse_coverage(verdict(), {"faq:1"})
        self.assertEqual(value.status, "sufficient")
        self.assertEqual(value.source_ids, ("faq:1",))

    def test_similarity_or_topic_match_is_not_a_complete_answer(self):
        value = parse_coverage(verdict("partial", [part(), part("WhatsApp included?", False, [])], "DIY WhatsApp inclusion"), {"faq:1"})
        self.assertEqual((value.status, value.part_count, value.supported_count), ("partial", 2, 1))

    def test_fabricated_source_is_rejected(self):
        self.assertEqual(parse_coverage(verdict(parts=[part(ids=["other-tenant:secret"])]), {"faq:1"}).status, "check_failed")

    def test_sufficient_without_citation_is_rejected(self):
        self.assertEqual(parse_coverage(verdict(parts=[part(ids=[])]), {"faq:1"}).status, "check_failed")

    def test_sufficient_cannot_hide_unsupported_part(self):
        self.assertEqual(parse_coverage(verdict(parts=[part(), part(supported=False, ids=[])]), {"faq:1"}).status, "check_failed")

    def test_empty_verdict_cannot_approve_business_facts(self):
        self.assertEqual(parse_coverage(verdict(parts=[]), set()).status, "check_failed")

    def test_short_contextual_reply_can_request_one_search(self):
        value = parse_coverage(verdict("insufficient", [part("Details from previous offer", False, [])], "Enterprise package brochure"), set())
        self.assertEqual(value.retry_query, "Enterprise package brochure")

    def test_hindi_query_is_preserved(self):
        value = parse_coverage(verdict("insufficient", [part("कीमत क्या है?", False, [])], "DIY प्लान की कीमत"), set())
        self.assertEqual(value.retry_query, "DIY प्लान की कीमत")

    def test_conflicting_sources_do_not_approve_answer(self):
        self.assertEqual(parse_coverage(verdict("conflicting", []), {"faq:1"}).status, "conflicting")

    def test_greeting_needs_no_business_evidence(self):
        self.assertEqual(parse_coverage(verdict("not_needed", []), set()).status, "not_needed")

    def test_malformed_json_is_not_no_evidence(self):
        for value in ("not json", "[]", "null", "{}", None):
            with self.subTest(value=value):
                self.assertEqual(parse_coverage(value, set()).status, "check_failed")

    def test_unknown_fields_or_actions_cannot_enter_contract(self):
        value = json.loads(verdict())
        value["crm_actions"] = [{"type": "move_stage"}]
        self.assertEqual(parse_coverage(json.dumps(value), {"faq:1"}).status, "check_failed")

    def test_query_length_is_bounded(self):
        self.assertEqual(parse_coverage(verdict(query="x" * 601), {"faq:1"}).status, "check_failed")

    def test_part_count_is_bounded(self):
        self.assertEqual(parse_coverage(verdict(parts=[part()] * 9), {"faq:1"}).status, "check_failed")

    def test_truthy_string_is_not_supported_boolean(self):
        self.assertEqual(parse_coverage(verdict(parts=[part(supported="true")]), {"faq:1"}).status, "check_failed")

    def test_unhashable_source_is_rejected(self):
        self.assertEqual(parse_coverage(verdict(parts=[part(ids=[{}])]), {"faq:1"}).status, "check_failed")

    def test_summary_contains_no_query_or_customer_question(self):
        value = parse_coverage(verdict(query="private search text"), {"faq:1"})
        self.assertNotIn("private", json.dumps(value.summary()))
        self.assertNotIn("question", value.summary())


@patch.dict(os.environ, FLAGS)
class RecoveryRoutingContracts(unittest.TestCase):
    def state(self, status="insufficient", **updates):
        return {"organization": SimpleNamespace(id="org-a"), "started_at": monotonic(),
                "evidence_coverage": Coverage(status, retry_query="DIY price"), "retrieval_query": "How much?", **updates}

    def test_only_allowlisted_organization_is_enabled(self):
        self.assertTrue(enabled(self.state()))
        self.assertFalse(enabled(self.state(organization=SimpleNamespace(id="org-b"))))

    def test_global_switch_is_required(self):
        with patch.dict(os.environ, {"AI_BRAIN_RECOVERY_ENABLED": "0"}):
            self.assertFalse(enabled(self.state()))

    def test_empty_allowlist_does_not_mean_all_organizations(self):
        with patch.dict(os.environ, {"AI_BRAIN_RECOVERY_ORGANIZATION_IDS": ""}):
            self.assertFalse(enabled(self.state()))

    def test_insufficient_or_partial_evidence_can_retry(self):
        for status in ("insufficient", "partial"):
            self.assertEqual(recovery_route(self.state(status)), "retry_retrieval")

    def test_sufficient_ambiguous_or_failed_assessment_does_not_retry(self):
        for status in ("sufficient", "ambiguous", "conflicting", "not_needed", "check_failed", "storage_error"):
            self.assertEqual(recovery_route(self.state(status)), "generate")

    def test_retry_never_loops(self):
        self.assertEqual(recovery_route(self.state(retrieval_retries=1)), "generate")

    def test_same_successful_query_is_not_repeated(self):
        self.assertEqual(recovery_route(self.state(retrieval_query="  DIY   PRICE ", retrieval_status="no_match")), "generate")

    def test_same_query_can_recover_a_technical_failure_once(self):
        self.assertEqual(recovery_route(self.state(retrieval_query="DIY price", retrieval_status="storage_error")), "retry_retrieval")

    def test_budget_expiry_disables_optional_recovery(self):
        self.assertEqual(recovery_route(self.state(started_at=monotonic() - 50)), "generate")

    def test_no_clock_or_invalid_budget_fails_closed(self):
        self.assertEqual(remaining({}), 0)
        for value in ("NaN", "inf", "invalid"):
            with patch.dict(os.environ, {"AI_BRAIN_RECOVERY_BUDGET_SECONDS": value}):
                self.assertEqual(remaining(self.state()), 0)

    def test_budget_is_capped(self):
        with patch.dict(os.environ, {"AI_BRAIN_RECOVERY_BUDGET_SECONDS": "999999"}):
            self.assertEqual(remaining({"started_at": 100}, now=100), 30)

    def test_future_clock_cannot_extend_budget(self):
        self.assertEqual(remaining({"started_at": 101}, now=100), 0)

    def test_missing_assessment_is_a_noop(self):
        state = self.state()
        state.pop("evidence_coverage")
        self.assertEqual(recovery_route(state), "generate")


if __name__ == "__main__":
    unittest.main()
