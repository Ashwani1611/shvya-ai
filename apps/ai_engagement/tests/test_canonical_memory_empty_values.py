"""Empty CRM fields do not suppress supported customer memory context."""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.ai_engagement.services.phase5_6_runtime import (
    _canonical_backend_facts,
    _canonical_memory_mutation,
)
from apps.ai_engagement.services.structured_memory import MemoryMutation


class CanonicalMemoryEmptyValuesTests(SimpleTestCase):
    def canonical(self, attributes, *, answered=False):
        requirements = [{"id": "daily-volume"}] if answered else []
        mappings = {"daily-volume": "leads_d"} if answered else {}
        states = {"daily-volume": {"status": "answered", "value": "11–30",
                  "source_message_id": "inbound-1", "raw_answer": "20 leads per day"}} if answered else {}
        profile = SimpleNamespace(
            revision="fixture-revision",
            as_dict=lambda: {"crm_capabilities": {"attributes": [
                {"key": "leads_d"}, {"key": "running_ads"},
            ]}},
            configured_requirements=lambda: requirements,
        )
        lead = SimpleNamespace(
            id="lead-1", attributes=attributes,
            _shvya_memory_requirement_mappings={"profile_revision": profile.revision, "mappings": mappings},
        )
        with patch("apps.ai_engagement.services.phase5_6_runtime._fresh_lead", return_value=lead), \
             patch("apps.ai_engagement.services.tenant_guard.TenantGuard.validate_current_lead_context"), \
             patch("apps.ai_engagement.services.organization_runtime_profile.get_organization_ai_runtime_profile", return_value=profile), \
             patch("apps.ai_engagement.services.qualification_state.requirements_for_lead", return_value=requirements), \
             patch("apps.ai_engagement.services.qualification_state.state_for_lead", return_value={"requirement_states": states}):
            return _canonical_backend_facts(organization=SimpleNamespace(id="org-1"), lead=lead)[0]

    def test_absent_none_and_blank_crm_values_do_not_become_canonical_facts(self):
        for attributes in ({}, {"leads_d": None, "running_ads": None},
                           {"leads_d": "", "running_ads": " \t\n "}):
            with self.subTest(attributes=attributes):
                self.assertEqual(self.canonical(attributes), {})

    def test_zero_false_and_nonblank_values_remain_authoritative(self):
        for attributes in ({"leads_d": 0, "running_ads": False},
                           {"leads_d": "10-30", "running_ads": "No"}):
            with self.subTest(attributes=attributes):
                facts = self.canonical(attributes)
                self.assertEqual({key: fact["value"] for key, fact in facts.items()}, attributes)
                self.assertTrue(all(fact["source_type"] == "verified_crm" for fact in facts.values()))
                mutation = _canonical_memory_mutation(
                    key="leads_d", old=facts["leads_d"], raw={"value": 20},
                    source_message_id="inbound-2", source_type="phase2_intent", mutation_cls=MemoryMutation,
                )
                self.assertFalse(mutation.accepted)
                self.assertEqual(mutation.reason, "canonical_backend_truth_conflict")

    def test_blank_mapped_field_does_not_hide_validated_qualification(self):
        for value in (None, "", "  "):
            with self.subTest(value=value):
                facts = self.canonical({"leads_d": value}, answered=True)
                self.assertEqual(facts["leads_d"]["value"], "11–30")
                self.assertEqual(facts["leads_d"]["source_type"], "validated_qualification")
                self.assertEqual(facts["leads_d"]["source_message_id"], "inbound-1")

    def test_nonblank_mapped_crm_field_still_overrides_qualification_memory(self):
        facts = self.canonical({"leads_d": "30+"}, answered=True)
        self.assertEqual(facts["leads_d"]["value"], "30+")
        self.assertEqual(facts["leads_d"]["source_type"], "verified_crm")
