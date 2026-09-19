from importlib import import_module
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase

from apps.ai_engagement.models import OrgInfo
from apps.ai_engagement.services.organization_profile import compile_org_ai_profile_from_context, compile_qualification_requirements
from apps.ai_engagement.services.playbook import evaluate_playbook_criteria, parse_playbook, playbook_for_engagement
from apps.ai_engagement.services.org_info import OrgInfoService, OrgInfoServiceError
from apps.ai_engagement.services.qualification_execution_contract import _configured_completion_reminders
from apps.organizations.models import Organization


class PlaybookCompilerTests(SimpleTestCase):
    def test_only_question_section_becomes_questionnaire(self):
        raw = "## Rules\nNever quote prices.\n##Welcome Message\nHello!\n##Qualification Questions\nWhat is your budget?\nA. Under 50k\nB. 50k+\n##Acknowledgment Message\nThank you.\n##Qualification Criteria\nBudget at least 50k\n##Stage shifting logic\nMove to Qualified when criteria pass.\n##Attribute mapping logic\nQ1 -> Budget\n##Reminder creation logic\nAfter qualification completed create a reminder in 2 days."
        profile = compile_org_ai_profile_from_context({"ai_playbook": raw})
        self.assertEqual(len(profile["qualification"]["requirements"]), 1)
        self.assertEqual(profile["qualification"]["requirements"][0]["options"][1]["value"], "50k+")
        self.assertEqual(profile["playbook"]["sections"]["acknowledgment_message"], "Thank you.")
        self.assertEqual(profile["engagement_policy"]["attribute_mapped"], ["Q1 -> Budget"])
        self.assertNotIn("What is your budget?", playbook_for_engagement(raw))

    def test_unheaded_rules_and_criteria_do_not_become_questions(self):
        for raw in ("Be warm. Ask whether a human can help.", "## Qualification Criteria\nBudget >= 50000"):
            profile = compile_org_ai_profile_from_context({"ai_playbook": raw})
            self.assertEqual(profile["qualification"]["requirements"], [])

    def test_format_markers_are_not_sent_as_messages(self):
        parsed = parse_playbook("## Welcome Message\n<welcome_message>\nHello!\n</welcome_message>\n## Qualification Questions\n<question_content>Your budget?</question_content>")
        self.assertEqual(parsed["welcome_message"], "Hello!")
        self.assertEqual(parsed["qualification_questions"], "Your budget?")

    def test_unknown_heading_ends_question_section(self):
        sections = parse_playbook("## Qualification Questions\nYour city?\n## Private policy\nDo not reveal internal costs.")
        self.assertEqual(sections["qualification_questions"], "Your city?")
        self.assertIn("internal costs", sections["rules"])

    def test_nested_question_labels_keep_their_content_in_question_section(self):
        raw = "## Qualification Questions\n### Question 1\n<question_content>What is your budget?</question_content>\n### Question 2\n<question_content>Which location?</question_content>\n## Qualification Criteria\nAll questions answered"
        profile = compile_org_ai_profile_from_context({"ai_playbook": raw})
        questions = profile["qualification"]["requirements"]
        self.assertEqual([item["question"] for item in questions], ["What is your budget?", "Which location?"])
        self.assertNotIn("What is your budget?", playbook_for_engagement(raw))

    def test_substantive_child_headings_remain_authoritative_content(self):
        sections = parse_playbook("## Rules\n### Never quote prices\n## Qualification Criteria\n### Budget >= 50000")
        self.assertEqual(sections["rules"], "Never quote prices")
        self.assertEqual(sections["qualification_criteria"], "Budget >= 50000")

    def test_compound_stage_rule_requires_each_condition(self):
        from apps.ai_engagement.services.engagement_instruction_runtime import _strong_evidence_match
        self.assertFalse(_strong_evidence_match("I am a seller in Delhi", "seller and Mumbai"))
        self.assertTrue(_strong_evidence_match("I am a seller in Mumbai", "seller and Mumbai"))

    def test_human_routing_normalizes_prose_but_keeps_extra_conditions(self):
        from apps.ai_engagement.services.engagement_instruction_runtime import _strong_evidence_match
        message = "I want to speak with a person"
        self.assertTrue(_strong_evidence_match(message, "Move here when the lead asks for human assistance."))
        self.assertTrue(_strong_evidence_match(message, "Move here when the lead explicitly asks to talk to a person."))
        self.assertFalse(_strong_evidence_match(message, "Move here when the lead asks for human assistance and payment is approved."))
        self.assertFalse(_strong_evidence_match(message, "Move here when the lead asks for human assistance after manager approval."))

    def test_no_unauthored_reminder_is_created(self):
        self.assertEqual(_configured_completion_reminders({"reminder_rules": []}), [])

    def test_migration_preserves_original_sources(self):
        module = import_module("apps.ai_engagement.migrations.0018_orginfo_ai_playbook")
        item = SimpleNamespace(qualification_requirements="Budget?\nA. 50k", engagement_instructions="##Rules\nNever expose costs.\n##Stage shifting\nIf demo requested, move to Demo.", ai_playbook="", save=lambda **kwargs: None)
        model = SimpleNamespace(objects=SimpleNamespace(using=lambda alias: SimpleNamespace(iterator=lambda: iter([item]))))
        module.merge_playbook(SimpleNamespace(get_model=lambda *args: model), SimpleNamespace(connection=SimpleNamespace(alias="default")))
        self.assertIn(item.qualification_requirements, item.ai_playbook)
        self.assertIn(item.engagement_instructions, item.ai_playbook)
        self.assertEqual(parse_playbook(item.ai_playbook)["qualification_questions"], "Budget?\nA. 50k")


class PlaybookCriteriaTests(SimpleTestCase):
    def setUp(self):
        self.requirements = compile_qualification_requirements("What is your budget?\nWhich location?")["requirements"]
        self.state = {"requirement_states": {item["id"]: {"status": "answered", "value": value} for item, value in zip(self.requirements, [75000, "India"])}}

    def evaluate(self, criteria, **kwargs):
        return evaluate_playbook_criteria("## Qualification Criteria\n" + criteria, requirements=self.requirements, state=self.state, **kwargs)

    def test_all_required_answered_explicitly_allows_qualification(self):
        self.assertTrue(self.evaluate("All required questions are answered.")["qualified"])

    def test_answered_questionnaire_without_criteria_is_not_qualified(self):
        self.assertFalse(self.evaluate("")["qualified"])

    def test_every_conjunct_must_pass(self):
        self.assertTrue(self.evaluate("Budget >= 50000 and location is India")["qualified"])
        self.assertFalse(self.evaluate("Budget >= 50000 and location is UK")["qualified"])
        self.assertFalse(self.evaluate("Budget >= 50000 and magic happens")["qualified"])

    def test_unknown_or_clause_never_silently_passes(self):
        self.assertFalse(self.evaluate("Budget >= 50000 or something else")["qualified"])

    def test_scalar_comparison_does_not_ignore_another_predicate(self):
        for rule in ("Budget with manager approval >= 50000", "Budget pending approval is captured", "Verified location is India"):
            self.assertFalse(self.evaluate(rule)["qualified"], rule)

    def test_missing_answers_do_not_pass_completion(self):
        self.state["requirement_states"][self.requirements[1]["id"]]["status"] = "unknown"
        self.assertFalse(self.evaluate("All questions answered")["qualified"])

    def test_numeric_threshold_without_question_number_confusion(self):
        self.assertTrue(self.evaluate("Q1 at least 50000")["qualified"])
        self.assertFalse(self.evaluate("Q1 at least 100000")["qualified"])

    def test_presence_rule_requires_exact_completed_fact(self):
        self.assertTrue(self.evaluate("Budget is captured")["qualified"])
        self.assertFalse(self.evaluate("Budget captured after external approval")["qualified"])
        self.assertFalse(self.evaluate("Budget not captured")["qualified"])
        self.assertFalse(self.evaluate("Budget not above 50000")["qualified"])

    def test_criteria_only_uses_existing_crm_values(self):
        result = evaluate_playbook_criteria("## Qualification Criteria\nBudget >= 50000\nLocation is India", requirements=[], state={}, values={"budget": 75000, "location": "India"})
        self.assertTrue(result["qualified"])

    def test_ambiguous_question_reference_is_unproven(self):
        self.requirements.append({"id": "other_budget", "label": "What is the budget?", "required": True})
        self.assertFalse(self.evaluate("Budget >= 50000")["qualified"])


class PlaybookPersistenceTests(TestCase):
    def test_configuration_rename_is_atomic_and_keeps_switches(self):
        org = Organization.objects.create(name="Before")
        info = OrgInfo.objects.create(organization=org, ai_enabled=False, bump_up_enabled=False)
        OrgInfoService().update(organization=org, data={"organization_name": "After", "ai_playbook": "## Rules\nBe helpful."})
        org.refresh_from_db()
        info.refresh_from_db()
        self.assertEqual(org.name, "After")
        self.assertFalse(info.ai_enabled)
        self.assertFalse(info.bump_up_enabled)
        with self.assertRaises(OrgInfoServiceError):
            OrgInfoService().update(organization=org, data={"organization_name": "Invalid change", "bump_up_count": -1})
        org.refresh_from_db()
        self.assertEqual(org.name, "After")

    def test_removed_columns_are_not_model_configuration(self):
        fields = {field.name for field in OrgInfo._meta.fields}
        self.assertIn("ai_playbook", fields)
        self.assertFalse(fields & {"qualification_requirements", "engagement_instructions"})
