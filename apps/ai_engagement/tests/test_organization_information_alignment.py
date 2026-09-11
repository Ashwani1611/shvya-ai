from __future__ import annotations

import json

from apps.ai_engagement.services.base_instructions import SHVYABaseInstructions
from apps.ai_engagement.services.context import AIContext
from apps.ai_engagement.services.engagement import EngagementService


def build_context() -> AIContext:
    return AIContext(
        organization={
            "id": "org-1",
            "name": "Acme Academy",
            "ai_enabled": True,
            "about": "Acme Academy provides cybersecurity training.",
            "bot_languages": "Hindi, English",
            "qualification_requirements": (
                "Which course are you interested in?\nWhat is your budget?"
            ),
            "engagement_instructions": (
                "Be concise and always end with one clear next step."
            ),
            "bump_up_enabled": False,
            "bump_up_count": 0,
        },
        lead={
            "id": "lead-1",
            "name": "Test Lead",
            "qualification": {
                "engagement_mode": "qualification",
                "qualification_status": "in_progress",
                "qualification_result": "",
                "qualified_stage_id": "stage-qualified",
                "current_requirement_id": "which_course_are_you_interested_in",
                "last_asked_requirement_id": "which_course_are_you_interested_in",
                "answered_requirement_ids": [],
                "qualification_answers": {},
                "processed_message_ids": [],
            },
        },
        pipeline={},
        stage={},
        contacts=[],
        attributes=[],
        conversation={"message_count": 0, "messages": []},
        conversation_summary=None,
        qualification_notes=[],
        knowledge=[],
    )


def normalize_whitespace(value: str) -> str:
    return " ".join(value.split())


def test_base_instructions_make_backend_state_authoritative_for_qualification():
    instructions = normalize_whitespace(SHVYABaseInstructions.get())

    assert "MANDATORY ORGANIZATION INFORMATION ALIGNMENT" in instructions
    assert "organization.about" in instructions
    assert "organization.bot_languages" in instructions
    assert "organization.qualification_requirements" in instructions
    assert "organization.engagement_instructions" in instructions
    assert "authoritative business configuration" in instructions
    assert "Unknown, unanswered, assumed, or merely implied criteria" in instructions
    assert "Never request a transition to the Qualified stage" in instructions
    assert "mandatory on EVERY customer-facing turn" in instructions
    assert "backend's current requirement" in instructions
    assert "do NOT use" in instructions


def test_engagement_input_keeps_org_facts_but_hides_full_questionnaire():
    context = build_context()
    payload = json.loads(EngagementService()._build_input(context=context))
    organization = payload["organization"]

    assert organization["about"] == context.organization["about"]
    assert organization["bot_languages"] == context.organization["bot_languages"]
    assert organization["engagement_instructions"] == context.organization["engagement_instructions"]
    assert "qualification_requirements" not in organization

    profile_qualification = organization["ai_profile"]["qualification"]
    assert "requirements" not in profile_qualification
    turn = payload["qualification_turn"]
    assert turn["current_requirement"]["id"] == "which_course_are_you_interested_in"
    assert "answered_requirement_ids" in turn


def test_engagement_system_prompt_prioritizes_backend_state_before_org_sequence_text():
    context = build_context()
    instructions = EngagementService()._build_instructions(context=context)
    normalized = normalize_whitespace(instructions)

    alignment_marker = "MANDATORY ORGANIZATION INFORMATION ALIGNMENT"
    task_marker = "SHVYA AI ENGAGEMENT TASK"

    assert alignment_marker in instructions
    assert context.organization["engagement_instructions"] in instructions
    assert instructions.index(alignment_marker) < instructions.index(task_marker)
    assert "MUST use a configured language" in normalized
    assert "conversation is primary evidence" in normalized
    assert "It does NOT make the lead authoritative" in normalized
    assert "backend qualification state" in normalized.casefold()
