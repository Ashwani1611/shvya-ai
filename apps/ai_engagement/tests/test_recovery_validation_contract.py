"""Credential-free contracts for question coverage, fixtures and safe comparison scopes."""
import copy
import json
import os
import sys
import unittest
from contextvars import copy_context
from dataclasses import dataclass, field
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from apps.ai_engagement.services import evidence_recovery as recovery
from apps.ai_engagement.services.recovery_scenarios import (
    RecoveryEvaluationError, check_preview, validate_scenarios,
)

ORG = "12345678-1234-1234-1234-123456789abc"
OTHER = "22345678-1234-1234-1234-123456789abc"


def fixture():
    return {"version": 1, "cases": [{"id": "pricing-hindi", "channel": "instagram",
        "turns": [{"message": "कीमत क्या है?", "expect": {"file_count": 0}}]}]}


@dataclass
class Context:
    organization: dict
    conversation: dict = field(default_factory=dict)
    lead: dict = field(default_factory=dict)


class CoverageDetailContracts(unittest.TestCase):
    def coverage(self):
        return recovery.parse_coverage(json.dumps({"status": "partial", "parts": [
            {"question": "What is the price?", "supported": True, "source_ids": ["faq:1"]},
            {"question": "WhatsApp included?", "supported": False, "source_ids": []},
        ], "retry_query": "DIY WhatsApp inclusion"}), {"faq:1"})

    def test_parts_survive_parsing(self):
        value = self.coverage()
        self.assertEqual(len(value.parts), 2)
        self.assertFalse(value.parts[1].supported)
        self.assertEqual(value.parts[0].source_ids, ("faq:1",))

    def test_prompt_retains_unanswered_part(self):
        value = self.coverage().prompt_dict()
        self.assertEqual(value["parts"][1]["question"], "WhatsApp included?")

    def test_summary_is_still_content_free(self):
        text = json.dumps(self.coverage().summary())
        self.assertNotIn("WhatsApp", text)
        self.assertNotIn("price", text)
        self.assertNotIn("retry_query", text)

    def test_mutating_prompt_does_not_change_immutable_parts(self):
        value = self.coverage()
        payload = value.prompt_dict()
        payload["parts"][0]["source_ids"].append("invented")
        self.assertEqual(value.parts[0].source_ids, ("faq:1",))

    def test_publish_keeps_source_language_and_rules(self):
        context = Context({"id": ORG, "bot_languages": "Hindi", "_file_candidates": [1]},
                          {"channel": "instagram"}, {"lead_source": "instagram"})
        state = {"organization": SimpleNamespace(id=ORG), "context": context,
                 "runtime_policy": {"engagement": {"rules": ["existing rule"]}}}
        with patch.object(recovery, "_trace") as trace:
            result = recovery._publish(state, self.coverage())
        policy = result["runtime_policy"]
        self.assertEqual(policy["engagement"], state["runtime_policy"]["engagement"])
        self.assertEqual(policy["knowledge_recovery"]["turn_context"]["channel"], "instagram")
        self.assertEqual(policy["knowledge_recovery"]["turn_context"]["bot_languages"], "Hindi")
        self.assertTrue(policy["knowledge_recovery"]["advisory_only"])
        self.assertEqual(result["context"].organization["_file_candidates"], [1])
        self.assertNotIn("parts", trace.call_args.kwargs)
        self.assertNotIn("knowledge_recovery", state["runtime_policy"])

    def test_fabricated_ids_still_rejected(self):
        payload = {"status": "sufficient", "parts": [{"question": "Price?", "supported": True,
                   "source_ids": ["other-tenant:1"]}], "retry_query": ""}
        self.assertEqual(recovery.parse_coverage(json.dumps(payload), {"faq:1"}).status, "check_failed")


class PreviewScopeContracts(unittest.TestCase):
    def setUp(self):
        class SandboxLead(SimpleNamespace):
            pass
        self.lead_type = SandboxLead
        module = ModuleType("apps.ai_engagement.services.playground")
        module._SandboxLead = SandboxLead
        self.modules = patch.dict(sys.modules, {module.__name__: module})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.flags = patch.dict(os.environ, {"AI_BRAIN_RECOVERY_ENABLED": "0",
                                            "AI_BRAIN_RECOVERY_ORGANIZATION_IDS": ""})
        self.flags.start()
        self.addCleanup(self.flags.stop)

    def state(self, org=ORG, lead=None):
        return {"organization": SimpleNamespace(id=org),
                "lead": lead or self.lead_type(id="playground:test", organization_id=org)}

    def test_preview_can_compare_without_changing_environment(self):
        original = dict(os.environ)
        with recovery.sandbox_recovery_preview(organization_id=ORG, use_recovery=True):
            self.assertTrue(recovery.enabled(self.state()))
        self.assertFalse(recovery.enabled(self.state()))
        self.assertEqual(dict(os.environ), original)

    def test_preview_cannot_enable_real_lead(self):
        with recovery.sandbox_recovery_preview(organization_id=ORG, use_recovery=True):
            self.assertFalse(recovery.enabled(self.state(lead=SimpleNamespace(id=ORG, organization_id=ORG))))

    def test_preview_is_tenant_scoped(self):
        with recovery.sandbox_recovery_preview(organization_id=ORG, use_recovery=True):
            self.assertFalse(recovery.enabled(self.state(OTHER)))
            self.assertFalse(recovery.enabled(self.state(lead=self.lead_type(organization_id=OTHER))))

    def test_preview_resets_on_exception(self):
        with self.assertRaises(RuntimeError):
            with recovery.sandbox_recovery_preview(organization_id=ORG, use_recovery=True):
                raise RuntimeError("test")
        self.assertIsNone(recovery._SANDBOX_RECOVERY.get())

    def test_nested_baseline_restores_recovery(self):
        with recovery.sandbox_recovery_preview(organization_id=ORG, use_recovery=True):
            with recovery.sandbox_recovery_preview(organization_id=ORG, use_recovery=False):
                self.assertFalse(recovery.enabled(self.state()))
            self.assertTrue(recovery.enabled(self.state()))

    def test_preexisting_other_context_not_affected(self):
        isolated = copy_context()
        with recovery.sandbox_recovery_preview(organization_id=ORG, use_recovery=True):
            self.assertFalse(isolated.run(recovery.enabled, self.state()))

    def test_invalid_scope_rejected(self):
        for org, flag in (("not-a-uuid", True), (ORG, "yes")):
            with self.subTest(org=org), self.assertRaises(ValueError):
                with recovery.sandbox_recovery_preview(organization_id=org, use_recovery=flag):
                    self.fail("Invalid preview was entered")


class FixtureContracts(unittest.TestCase):
    def test_hindi_is_preserved(self):
        self.assertEqual(validate_scenarios(fixture())[0]["turns"][0]["message"], "कीमत क्या है?")

    def test_channels_are_preview_only(self):
        for channel in ("whatsapp", "instagram", "sandbox"):
            data = fixture(); data["cases"][0]["channel"] = channel
            self.assertEqual(validate_scenarios(data)[0]["channel"], channel)

    def test_no_remote_execution_or_arbitrary_actions(self):
        for key in ("send", "tools", "url", "execute", "organization_id"):
            data = fixture(); data["cases"][0][key] = True
            with self.subTest(key=key), self.assertRaises(RecoveryEvaluationError):
                validate_scenarios(data)

    def test_invalid_channel_shapes_rejected(self):
        for channel in ({}, [], None, "hosted-send"):
            data = fixture(); data["cases"][0]["channel"] = channel
            with self.subTest(channel=channel), self.assertRaises(RecoveryEvaluationError):
                validate_scenarios(data)

    def test_invalid_root_rejected(self):
        for data in (None, [], {}, {"version": True, "cases": []}):
            with self.subTest(data=data), self.assertRaises(RecoveryEvaluationError):
                validate_scenarios(data)

    def test_duplicate_case_ids_rejected(self):
        data = fixture(); data["cases"].append(copy.deepcopy(data["cases"][0]))
        with self.assertRaises(RecoveryEvaluationError): validate_scenarios(data)

    def test_injected_case_label_rejected(self):
        data = fixture(); data["cases"][0]["id"] = "x\nPRIVATE_DATA"
        with self.assertRaises(RecoveryEvaluationError): validate_scenarios(data)

    def test_oversized_messages_rejected(self):
        data = fixture(); data["cases"][0]["turns"][0]["message"] = "x" * 4001
        with self.assertRaises(RecoveryEvaluationError): validate_scenarios(data)

    def test_unsupported_checks_rejected(self):
        data = fixture(); data["cases"][0]["turns"][0]["expect"] = {"python": "print(1)"}
        with self.assertRaises(RecoveryEvaluationError): validate_scenarios(data)

    def test_boolean_file_count_rejected(self):
        data = fixture(); data["cases"][0]["turns"][0]["expect"] = {"file_count": True}
        with self.assertRaises(RecoveryEvaluationError): validate_scenarios(data)

    def test_internal_attribute_expectations_rejected(self):
        data = fixture(); data["cases"][0]["turns"][0]["expect"] = {"attributes": {"shvya_ai_runtime": "secret"}}
        with self.assertRaises(RecoveryEvaluationError): validate_scenarios(data)

    def test_turn_limit_is_enforced(self):
        data = fixture(); data["cases"][0]["turns"] *= 7
        with self.assertRaises(RecoveryEvaluationError): validate_scenarios(data)

    def test_checks_are_redacted(self):
        result = SimpleNamespace(response="PRIVATE_REPLY ₹2999", files=[{"url": "PRIVATE_URL"}],
            events=[{"type": "attribute_updates", "value": "PRIVATE_LEAD_DATA"}], stage={"name": "Qualified"})
        report = check_preview(result=result,
            expected={"contains_all": ["PRIVATE_REPLY"], "excludes": ["missing"], "file_count": 1,
                      "stage": "Qualified", "attributes": {"budget": "PRIVATE_VALUE"}},
            attributes={"budget": "PRIVATE_VALUE"})
        self.assertTrue(report["passed"])
        self.assertNotIn("PRIVATE", json.dumps(report))
        self.assertNotIn("url", json.dumps(report))

    def test_unscored_turn_is_not_a_passing_accuracy_test(self):
        result = SimpleNamespace(response="Hello", events=[], files=[], stage={})
        self.assertIsNone(check_preview(result=result, expected={}, attributes={})["passed"])

    def test_failed_expectation_is_counted(self):
        result = SimpleNamespace(response="Hello", events=[], files=[], stage={})
        report = check_preview(result=result, expected={"contains_all": ["price"], "file_count": 1}, attributes={})
        self.assertEqual(report["failed_checks"], 2)
        self.assertFalse(report["passed"])


if __name__ == "__main__":
    unittest.main()
