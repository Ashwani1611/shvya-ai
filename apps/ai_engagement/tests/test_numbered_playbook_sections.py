"""Numbered authoring headings preserve section boundaries and customer copy."""

import unittest

from apps.ai_engagement.services.playbook import parse_playbook


class NumberedPlaybookSectionsTests(unittest.TestCase):
    def test_numbered_sections_preserve_copy_and_policy(self):
        raw = (
            "# Rules\nNever invent prices.\n"
            "## 23. Welcome Message\n<welcome_message>Hello from our club.</welcome_message>\n"
            "Rules: send only once.\n"
            "## 24. Qualification Questions\n<question_content>Which program?</question_content>\n"
            "## 25. Qualification Criteria\nAll required questions answered.\n"
            "## 26. Stage shifting logic\nMove after confirmation.\n"
        )
        sections = parse_playbook(raw)
        self.assertEqual(sections["welcome_message"], "Hello from our club.")
        self.assertEqual(sections["qualification_questions"], "Which program?")
        self.assertEqual(sections["qualification_criteria"], "All required questions answered.")
        self.assertEqual(sections["stage_shifting"], "Move after confirmation.")
        self.assertIn("send only once", sections["rules"])
        self.assertNotIn("send only once", sections["welcome_message"])

    def test_numbered_unknown_heading_ends_question_section(self):
        raw = "## 1) Qualification Questions\nYour city?\n## 2) Private policy\nNever reveal costs."
        sections = parse_playbook(raw)
        self.assertEqual(sections["qualification_questions"], "Your city?")
        self.assertIn("Never reveal costs", sections["rules"])

    def test_numbered_policy_lines_do_not_create_sections(self):
        sections = parse_playbook("# Rules\n1. Welcome Message\nNever send this policy.")
        self.assertEqual(sections["welcome_message"], "")
        self.assertIn("Never send this policy", sections["rules"])

    def test_existing_unnumbered_sections_remain_supported(self):
        sections = parse_playbook("## Welcome Message\nHello.\n## Qualification Questions\nYour goal?")
        self.assertEqual(sections["welcome_message"], "Hello.")
        self.assertEqual(sections["qualification_questions"], "Your goal?")
