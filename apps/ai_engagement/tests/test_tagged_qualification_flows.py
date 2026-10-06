"""Explicit qualification flows compile only their authored customer questions."""
from django.test import SimpleTestCase
from apps.ai_engagement.services.playbook import parse_playbook
from apps.ai_engagement.services.organization_profile import compile_qualification_requirements


class TaggedQualificationFlowTests(SimpleTestCase):
    def test_explicit_flow_preserves_question_options_without_policy(self):
        raw = ("# Enquiry Qualification Flow\n## 1. Programme\n<question_content>\n"
               "Which programme interests you?\n1. Alpha\n2. Beta\n</question_content>\n"
               "When selected: set internal tag.\n# Stage Rules\nNever expose CRM.")
        sections = parse_playbook(raw)
        compiled = compile_qualification_requirements(sections["qualification_questions"])
        self.assertEqual(len(compiled["requirements"]), 1)
        self.assertEqual([option["value"] for option in compiled["requirements"][0]["options"]],
                         ["Alpha", "Beta"])
        self.assertNotIn("internal", sections["qualification_questions"])
        self.assertIn("set internal tag", sections["rules"])

    def test_compound_capture_becomes_separate_requirements(self):
        raw = ("# Registration Qualification Flow\n<question_content>\n"
               "Thank you for the confirmation. Please share:\n"
               "1. Full Name\n2. Age\n3. Preferred Date (DD/MM)\n</question_content>")
        questions = parse_playbook(raw)["qualification_questions"]
        compiled = compile_qualification_requirements(questions)
        self.assertEqual(len(compiled["requirements"]), 3)
        self.assertTrue(all(not item["options"] for item in compiled["requirements"]))
        self.assertIn("Full Name", compiled["requirements"][0]["question"])
        self.assertIn("Preferred Date (DD/MM)", compiled["requirements"][2]["question"])
        self.assertNotIn("Thank you", questions)

    def test_example_flow_does_not_create_requirements(self):
        raw = ("# Example Qualification Flow\n<question_content>\n"
               "Which example product?\n</question_content>")
        self.assertEqual(parse_playbook(raw)["qualification_questions"], "")

    def test_untagged_custom_flow_is_not_promoted(self):
        raw = "# Enquiry Qualification Flow\nAsk about internal routing and budget."
        self.assertEqual(parse_playbook(raw)["qualification_questions"], "")

    def test_appointment_template_does_not_become_static_question(self):
        raw = ("# Enquiry Qualification Flow\n<question_content>\nWhat is your goal?\n"
               "</question_content>\n# Appointment Flow\n<question_content>\n"
               "Available slots: {available_valid_slots}\n</question_content>")
        questions = parse_playbook(raw)["qualification_questions"]
        self.assertEqual(questions, "What is your goal?")
        self.assertNotIn("available_valid_slots", questions)

    def test_specific_choice_request_keeps_options(self):
        raw = ("## Qualification Questions\n<question_content>\n"
               "Please share your preferred product:\n1. Alpha\n2. Beta\n</question_content>")
        compiled = compile_qualification_requirements(
            parse_playbook(raw)["qualification_questions"])
        self.assertEqual(len(compiled["requirements"]), 1)
        self.assertEqual(len(compiled["requirements"][0]["options"]), 2)
