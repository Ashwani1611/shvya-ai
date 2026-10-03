"""Source-contract regressions, not model, renderer or delivery evaluations.

These tests intentionally need only the standard library. Run directly with
`python tests/test_customer_content_contract.py`, or through repository pytest.
"""
from pathlib import Path
import re
import runpy
import unittest


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "apps/integrations/operations/setup_assets"
SKILLS = ("shvya-whatsapp", "shvya-ai-playbook", "shvya-cadence-builder")


def read_asset(path: str) -> str:
    return (ASSETS / path).read_text(encoding="utf-8")


class CustomerContentContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        namespace = runpy.run_path(str(ROOT / "apps/ai_engagement/prompts/engagement.py"))
        cls.prompt = namespace["CUSTOMER_ENGAGEMENT_INSTRUCTIONS"]
        cls.response = cls.prompt.split("\nRESPONSE BEHAVIOR\n", 1)[1].split(
            "\nLEAD QUESTIONS AND GUIDANCE\n", 1
        )[0]
        cls.gates = read_asset("framework/customer-content-gates.md")
        cls.quality = read_asset("framework/skill-quality-contract.md")

    def test_runtime_prompt_has_plain_text_default_not_casual_emphasis_permission(self):
        self.assertIn("plain text", self.response)
        self.assertNotIn("WhatsApp *bold* and", self.response)
        self.assertNotIn("_italics_ may be used sparingly", self.response)
        for marker in ("Markdown/WhatsApp emphasis", "HTML", "tables", "code fences"):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.response)

    def test_mobile_layout_does_not_truncate_or_reorder_options(self):
        for instruction in (
            "blank line between ideas", "one reply option", "meaning and order",
            "Do not hard-wrap", "cut necessary answers",
        ):
            with self.subTest(instruction=instruction):
                self.assertIn(instruction, self.response)

    def test_runtime_keeps_literals_and_resolved_personalization_distinct(self):
        for instruction in (
            "Preserve verified URLs, names, numbers and technical terms exactly",
            "asterisk or underscore inside a URL or identifier",
            "verified name", "never guess", "unresolved placeholder",
            "Authoring tokens belong in saved drafts",
        ):
            with self.subTest(instruction=instruction):
                self.assertIn(instruction, self.response)

    def test_customer_rules_do_not_replace_required_machine_output(self):
        self.assertIn("customer-facing message value", self.response)
        self.assertIn("required output JSON", self.response)
        output = self.prompt.split("\nOUTPUT\n", 1)[1]
        self.assertIn("Return ONLY a valid JSON object", output)
        for key in (
            "should_engage", "silence_rule", "message", "file_document_id",
            "crm_actions", "qualification_updates", "next_requirement_id", "reason_code",
        ):
            with self.subTest(key=key):
                self.assertIn(f'"{key}":', output)
        self.assertIn('If should_engage is false, message MUST be ""', output)

    def test_existing_channel_and_qualification_safeguards_remain(self):
        for instruction in (
            "recent_conversation.channel", "A phone number is not required for an Instagram",
            "execution_mode is preview", "Never ask any answered requirement again",
            "Do not use NO_ACTION merely because the message is short",
            "Platform opt-out and human-lock gates are authoritative",
            "never claim the file was sent",
        ):
            with self.subTest(instruction=instruction):
                self.assertIn(instruction, self.prompt)

    def test_shared_gates_distinguish_markdown_documents_from_customer_copy(self):
        self.assertIn("Internal SKILL.md files", self.gates)
        self.assertIn("selective bold", self.gates)
        self.assertIn("plain text with short paragraphs", self.gates)
        self.assertIn("Never run customer-text cleanup over an entire skill", self.gates)
        self.assertIn("content-rules.md", self.gates)

    def test_first_name_authoring_and_missing_name_preview_are_separate(self):
        for instruction in (
            "{{lead_first_name}}", "first-name mapping", "populated-name and missing-name",
            "Final rendered customer text", "existing verified missing-name fallback",
            "block that item", "Company facts, internal notes, titles",
        ):
            with self.subTest(instruction=instruction):
                self.assertIn(instruction, self.gates)

    def test_template_parser_and_literal_content_are_protected(self):
        for instruction in (
            "Do not delete every asterisk, underscore or brace",
            "Playbook headings/message tags", "Approved API templates",
            "approved text and supported provider mappings", "Unicode text",
            "historical conversations", "exact approved copy",
        ):
            with self.subTest(instruction=instruction):
                self.assertIn(instruction, self.gates)

    def test_customer_copy_owners_link_directly_to_shared_gates(self):
        for skill in SKILLS:
            path = ASSETS / "skills" / skill / "SKILL.md"
            content = path.read_text(encoding="utf-8")
            with self.subTest(skill=skill):
                self.assertIn("## Customer-copy formatting", content)
                links = re.findall(r"\]\(([^)]+customer-content-gates\.md)\)", content)
                self.assertEqual(len(links), 1)
                self.assertEqual(
                    (path.parent / links[0]).resolve(),
                    (ASSETS / "framework/customer-content-gates.md").resolve(),
                )
                self.assertTrue((path.parent / links[0]).is_file())

    def test_edited_skills_retain_valid_frontmatter_and_markdown_structure(self):
        for skill in SKILLS:
            content = read_asset(f"skills/{skill}/SKILL.md")
            with self.subTest(skill=skill):
                match = re.match(r"\A---\nname: ([a-z0-9-]+)\ndescription: ([^\n]+)\n---\n", content)
                self.assertIsNotNone(match)
                self.assertEqual(match.group(1), skill)
                self.assertLessEqual(len(match.group(1)), 64)
                self.assertLessEqual(len(match.group(2)), 1024)
                for heading in ("## Workflow", "## Guardrails", "## Output", "## Shared quality contract"):
                    self.assertIn(heading, content)

    def test_operator_report_is_readable_and_destination_aware(self):
        self.assertIn("## 11. Readable operator output", self.quality)
        for label in ("Status", "What was checked", "Findings", "Changes made", "Verification", "Remaining gaps"):
            with self.subTest(label=label):
                self.assertIn(label, self.quality)
        self.assertIn("Omit empty sections", self.quality)
        self.assertIn("present the operator report as plain text too", self.quality)
        self.assertIn("never paste a diagnostic report into a lead message", self.quality)

    def test_verification_does_not_overclaim_runtime_or_delivery(self):
        for instruction in (
            "PASS, FAIL or UNKNOWN", "Do not count an untested surface as passed",
            "prompt/source contract test proves instruction consistency",
            "not that a model obeyed it", "runtime formatter enforced it",
            "provider acceptance and recipient delivery as separate evidence",
            "does not authorize sends, enrollment, activation, timing changes or routing changes",
        ):
            with self.subTest(instruction=instruction):
                self.assertIn(instruction, self.gates)
        for surface in ("API", "Coexistence", "Hosted", "Instagram", "Sandbox"):
            with self.subTest(surface=surface):
                self.assertIn(surface, self.gates)


if __name__ == "__main__":
    unittest.main(verbosity=2)
