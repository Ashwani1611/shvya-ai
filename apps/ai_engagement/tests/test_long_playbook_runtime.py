"""Long saved Playbooks keep their tail without unbounded prompt expansion."""
import json
from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.ai_engagement.services.engagement import EngagementService
from apps.ai_engagement.services.organization_profile import compile_org_ai_profile_from_context
from apps.ai_engagement.services.playbook import MAX_PLAYBOOK_CHARS, validate_playbook
from apps.ai_engagement.services.response_composer import build_response_plan
from apps.ai_engagement.services.turn_controller import build_turn_policy
from apps.ai_engagement.tests.test_organization_information_alignment import build_context


class LongPlaybookRuntimeTests(SimpleTestCase):
    def playbook(self):
        return (
            "## Rules\n" + "Use approved company information.\n" * 1750
            + "## Reminder creation logic\n"
            + "Ask for the agreed future callback time before creating a reminder."
            + "\nTAIL_POLICY_MARKER"
        )

    def test_turn_policy_keeps_every_supported_character_including_last_one(self):
        raw = "## Rules\n" + "x" * (MAX_PLAYBOOK_CHARS - len("## Rules\n") - 1) + "Z"
        self.assertEqual(validate_playbook(raw), raw)
        policy = build_turn_policy(context=SimpleNamespace(organization={"ai_playbook": raw}, stage={}))
        self.assertEqual(policy.operating_spec, raw)

    def test_oversized_source_is_rejected_without_silent_prefix_truncation(self):
        raw = "x" * (MAX_PLAYBOOK_CHARS + 1)
        with self.assertRaisesRegex(ValueError, "100,000 characters"):
            validate_playbook(raw)
        with self.assertRaisesRegex(ValueError, "100,000 characters"):
            build_turn_policy(context=SimpleNamespace(organization={"ai_playbook": raw}, stage={}))
        with self.assertRaisesRegex(ValueError, "100,000 characters"):
            build_response_plan(payload={"organization": {"id": "org", "ai_playbook": raw},
                "lead": {"id": "lead"}}, organization_id="org", lead_id="lead")

    def test_draft_and_final_composition_keep_tail_and_language_policy(self):
        context = build_context()
        raw = self.playbook()
        self.assertGreater(raw.index("TAIL_POLICY_MARKER"), 50000)
        context.organization["ai_playbook"] = raw
        service = EngagementService()
        instructions = service._build_instructions(context=context)
        payload = json.loads(service._build_input(context=context))
        self.assertIn(raw, instructions)
        self.assertEqual(instructions.count("TAIL_POLICY_MARKER"), 1)
        self.assertIn("backend-owned current", instructions)
        self.assertIn("MUST use a configured language", instructions)
        for final in (False, True):
            with self.subTest(final_composition=final):
                plan = build_response_plan(payload=payload, organization_id="org-1", lead_id="lead-1",
                    final_composition=final)
                self.assertIn("ORGANIZATION OPERATING SPEC", plan.organization_instructions)
                self.assertLess(len(plan.organization_instructions), 200)
                self.assertEqual(plan.allowed_languages, ("Hindi", "English"))

    def test_standalone_composition_keeps_long_policy_without_system_flag(self):
        raw = self.playbook()
        for final in (False, True):
            with self.subTest(final_composition=final):
                plan = build_response_plan(payload={"organization": {"id": "org", "ai_playbook": raw},
                    "lead": {"id": "lead"}}, organization_id="org", lead_id="lead", final_composition=final)
                self.assertEqual(plan.organization_instructions, raw)

    def test_distinct_compiled_instructions_are_not_removed_as_duplicate(self):
        context = build_context()
        context.organization["ai_playbook"] = self.playbook()
        profile = compile_org_ai_profile_from_context(context.organization)
        profile["communication"]["custom_instructions"] = "Additional backend-prepared instruction."
        instructions = EngagementService()._build_instructions(context=context, profile=profile)
        self.assertIn("Additional backend-prepared instruction.", instructions)
        self.assertIn(context.organization["ai_playbook"], instructions)

    def test_distinct_composition_instructions_are_not_replaced_by_raw_reference(self):
        extra = self.playbook() + "\nAdditional backend-prepared instruction."
        plan = build_response_plan(payload={
            "organization": {"id": "org", "ai_playbook": self.playbook(),
                "ai_profile": {"communication": {"custom_instructions": extra}}},
            "organization_operating_spec": {"playbook_in_system_instructions": True},
            "lead": {"id": "lead"},
        }, organization_id="org", lead_id="lead")
        self.assertEqual(plan.organization_instructions, extra)

    def test_medium_system_playbook_does_not_expand_each_composition_plan(self):
        from copy import deepcopy

        raw = "## Rules\n" + "Use approved company information.\n" * 750 + "FINAL_AUTHORED_RULE"
        self.assertLess(len(raw), 50000)
        payload = {
            "organization": {"id": "org", "ai_playbook": raw,
                "about": "Approved company facts. FINAL_ABOUT_FACT",
                "bot_languages": "English, Hinglish"},
            "organization_operating_spec": {"playbook_in_system_instructions": True},
            "lead": {"id": "lead", "qualification": {
                "engagement_mode": "qualification",
                "requirement_states": {"goal": {"status": "unknown"}}}},
            "next_requirement": {"id": "goal", "question": "What is your goal?"},
            "grounding": {"sensitive": False},
        }
        before = deepcopy(payload)
        for final in (False, True):
            with self.subTest(final_composition=final):
                plan = build_response_plan(payload=payload,
                    organization_id="org", lead_id="lead", final_composition=final)
                self.assertLess(len(plan.organization_instructions), 200)
                self.assertIn("ORGANIZATION OPERATING SPEC", plan.organization_instructions)
                self.assertEqual(plan.next_question, payload["next_requirement"])
                self.assertEqual(plan.allowed_languages, ("English", "Hinglish"))
                self.assertIn("FINAL_ABOUT_FACT", plan.allowed_facts[-1]["content"])
        self.assertEqual(payload, before)
        self.assertTrue(payload["organization"]["ai_playbook"].endswith("FINAL_AUTHORED_RULE"))

    def test_medium_standalone_playbook_is_not_replaced_by_a_missing_system_spec(self):
        raw = "## Rules\n" + "Use approved company information.\n" * 750 + "FINAL_AUTHORED_RULE"
        for flag in (None, False):
            with self.subTest(system_spec_flag=flag):
                plan = build_response_plan(payload={
                    "organization": {"id": "org", "ai_playbook": raw},
                    "organization_operating_spec": {"playbook_in_system_instructions": flag},
                    "lead": {"id": "lead"},
                }, organization_id="org", lead_id="lead")
                self.assertEqual(plan.organization_instructions, raw)
