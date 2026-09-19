"""Legacy authoring migration must not erase or weaken qualification rules."""
from importlib import import_module
from types import SimpleNamespace

from apps.ai_engagement.services.organization_profile import compile_qualification_requirements
from apps.ai_engagement.services.playbook import evaluate_playbook_criteria, parse_playbook


def migrate(instructions, questions=""):
    migration = import_module("apps.ai_engagement.migrations.0018_orginfo_ai_playbook")
    item = SimpleNamespace(qualification_requirements=questions, engagement_instructions=instructions,
                           ai_playbook="", save=lambda **kwargs: None)
    model = SimpleNamespace(objects=SimpleNamespace(using=lambda alias: SimpleNamespace(iterator=lambda: iter([item]))))
    migration.merge_playbook(SimpleNamespace(get_model=lambda *args: model),
                            SimpleNamespace(connection=SimpleNamespace(alias="default")))
    return item.ai_playbook


def test_mixed_legacy_criteria_preserves_threshold_instead_of_turning_it_into_question():
    raw = migrate("## Qualification Criteria\nWhat is your budget?\nQualify when all questions answered\nBudget >= 50000")
    sections = parse_playbook(raw)
    assert sections["qualification_questions"] == "What is your budget?"
    assert "Budget >= 50000" in sections["qualification_criteria"]
    requirements = compile_qualification_requirements(sections["qualification_questions"])["requirements"]
    state = {"requirement_states": {requirements[0]["id"]: {"status": "answered", "value": 5000}}}
    assert evaluate_playbook_criteria(raw, requirements=requirements, state=state)["qualified"] is False


def test_nested_question_labels_and_options_move_without_losing_following_rules():
    raw = migrate("## Qualification Criteria\n### Question 1\nWhat is your budget?\nA. 5000\nB. 50000\n"
                  "### Question 2\nWhich location?\nBudget >= 50000\nLocation is India\n"
                  "## Stage shifting logic\nWhen qualified, move to Qualified.")
    sections = parse_playbook(raw)
    assert "What is your budget?" in sections["qualification_questions"]
    assert "A. 5000" in sections["qualification_questions"]
    assert "Which location?" in sections["qualification_questions"]
    assert "Budget >= 50000" in sections["qualification_criteria"]
    assert "Location is India" in sections["qualification_criteria"]
    assert "When qualified, move to Qualified." in sections["stage_shifting"]


def test_ambiguous_nonquestion_lines_stay_authoritative_and_fail_closed():
    raw = migrate("## Qualification Criteria\nWhat is your budget?\nAll questions answered\n"
                  "Verify manager approval before proceeding.\nBudget >= 50000?\nBudget approved?")
    sections = parse_playbook(raw)
    assert "Verify manager approval before proceeding." in sections["qualification_criteria"]
    assert "Budget >= 50000?" in sections["qualification_criteria"]
    assert "Budget approved?" in sections["qualification_criteria"]
    requirements = compile_qualification_requirements(sections["qualification_questions"])["requirements"]
    state = {"requirement_states": {requirements[0]["id"]: {"status": "answered", "value": 100000}}}
    assert evaluate_playbook_criteria(raw, requirements=requirements, state=state)["qualified"] is False


def test_existing_separate_sources_and_missing_criteria_are_preserved_conservatively():
    instructions = "## Rules\nNever quote prices.\n## Stage shifting logic\nMove to Qualified after completion."
    questions = "What is your budget?\nA. 5000\nB. 50000"
    raw = migrate(instructions, questions)
    assert instructions in raw
    assert questions in raw
    assert parse_playbook(raw)["qualification_criteria"] == ""
    assert evaluate_playbook_criteria(raw, requirements=[], state={})["qualified"] is False
