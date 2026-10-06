"""Prompt-size regressions retain one authoritative copy of authored policy."""
import json
from copy import deepcopy
from django.test import SimpleTestCase

from apps.ai_engagement.services.engagement import EngagementService
from apps.ai_engagement.services.organization_profile import compile_org_ai_profile_from_context
from apps.ai_engagement.services.prompt_overrides import _deduplicate_operating_spec
from apps.ai_engagement.services.response_composer import build_response_plan
from apps.ai_engagement.tests.test_organization_information_alignment import build_context


class OperatingSpecDeduplicationTests(SimpleTestCase):
    def test_medium_playbook_keeps_complete_system_source_once(self):
        context = build_context()
        raw = "## Rules\n" + "Follow the approved rules.\n" * 900 + "MIDDLE_POLICY_MARKER\nTAIL_POLICY_MARKER"
        context.organization["ai_playbook"] = raw
        instructions = EngagementService()._build_instructions(context=context)
        self.assertIn(raw, instructions)
        self.assertEqual(instructions.count("MIDDLE_POLICY_MARKER"), 1)
        self.assertEqual(instructions.count("TAIL_POLICY_MARKER"), 1)

    def test_duplicate_about_and_sections_are_removed_without_losing_facts(self):
        context = build_context()
        service = EngagementService()
        payload = json.loads(service._build_input(context=context))
        self.assertEqual(payload["organization"]["about"], context.organization["about"])
        self.assertNotIn("about", payload["organization_operating_spec"])
        self.assertNotIn("sections", payload["organization"]["ai_profile"]["playbook"])
        plan = build_response_plan(payload=payload, organization_id=str(context.organization["id"]),
            lead_id=str(context.lead["id"]))
        self.assertTrue(any(context.organization["about"][:30] in str(item) for item in plan.allowed_facts))

    def test_distinct_compiled_sections_are_retained(self):
        source = {"ai_playbook": "## Rules\nUse approved information."}
        payload = {"organization_operating_spec": {"playbook_in_system_instructions": True},
            "organization": {"ai_profile": {"playbook": {"sections": {"rules": "Distinct backend guidance"}}}}}
        _deduplicate_operating_spec(payload, source)
        self.assertEqual(payload["organization"]["ai_profile"]["playbook"]["sections"]["rules"],
            "Distinct backend guidance")

    def test_standalone_payload_without_system_boundary_is_unchanged(self):
        source = {"ai_playbook": "## Rules\nUse approved information."}
        profile = compile_org_ai_profile_from_context(source)
        payload = {"organization_operating_spec": {"about": "Business facts"},
            "organization": {"about": "Business facts", "ai_profile": profile}}
        before = deepcopy(payload)
        _deduplicate_operating_spec(payload, source)
        self.assertEqual(payload, before)


class CustomStageHeadingTests(SimpleTestCase):
    def test_crm_stage_rules_are_compiled_for_explicit_handoff(self):
        from apps.ai_engagement.services.engagement_instruction_policy import compile_engagement_instruction_policy
        raw = ("# CRM STAGE RULES\n"
               "## 34. Call Requested\n"
               "Move to Call Requested when the lead explicitly asks for a human.\n"
               "# OTHER POLICY\nNever invent prices.")
        policy = compile_engagement_instruction_policy(raw)
        self.assertTrue(any("Call Requested" in item and "human" in item
                            for item in policy["stage_shifting"]))
        self.assertFalse(any("invent prices" in item for item in policy["stage_shifting"]))

    def test_plain_policy_mention_does_not_create_stage_section(self):
        from apps.ai_engagement.services.engagement_instruction_policy import compile_engagement_instruction_policy
        raw = "# Rules\nThe phrase CRM stage rules is only a reference.\nAnswer the customer."
        self.assertEqual(compile_engagement_instruction_policy(raw)["stage_shifting"], [])
