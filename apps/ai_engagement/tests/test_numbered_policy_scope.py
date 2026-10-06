"""Numbered policy headings retain the conditions that govern their action."""
from django.test import SimpleTestCase
from apps.ai_engagement.services.playbook import parse_playbook, policy_blocks
from apps.ai_engagement.services.engagement_instruction_policy import compile_engagement_instruction_policy


class NumberedPolicyScopeTests(SimpleTestCase):
    def test_numbered_stage_rule_keeps_destination_with_trigger(self):
        raw = ("# Stage shifting logic\n## 1. Consultation\n"
               "Move to:\n**Consultation Requested**\n"
               "when the lead explicitly asks for:\n- A human\n- A consultant\n"
               "Never promise immediate availability.\n"
               "## 2. Qualified\nOnce:\n- All mandatory criteria pass\n"
               "move to:\n**Qualified**")
        rules = compile_engagement_instruction_policy(raw)["stage_shifting"]
        self.assertEqual(len(rules), 2)
        self.assertIn("Consultation Requested", rules[0])
        self.assertIn("explicitly asks", rules[0])
        self.assertIn("Never promise", rules[0])
        self.assertNotIn("mandatory criteria", rules[0])
        self.assertIn("mandatory criteria", rules[1])

    def test_nested_numbered_condition_does_not_start_a_new_rule(self):
        raw = ("# Stage shifting logic\n## 1. Visit\nMove to Visit Requested only if:\n"
               "### 1. Confirmed intent\nThe customer requests a visit.\n"
               "### 2. Exception\nNever move after an opt-out.\n"
               "## 2. Other route\nKeep the current stage.")
        rules = policy_blocks(parse_playbook(raw)["stage_shifting"])
        self.assertEqual(len(rules), 2)
        self.assertIn("Never move after an opt-out", rules[0])
        self.assertNotIn("Other route", rules[0])

    def test_legacy_named_rules_keep_existing_grouping(self):
        source = ("Rule 1. Pricing\nDo not invent prices.\n"
                  "Rule 2. Callback\nOffer the approved team contact.")
        self.assertEqual(policy_blocks(source), [
            "Rule 1. Pricing\nDo not invent prices.",
            "Rule 2. Callback\nOffer the approved team contact.",
        ])
