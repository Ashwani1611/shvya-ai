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
                "Confirm course interest, budget, and joining timeline."
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


def test_base_instructions_make_all_organization_information_authoritative():
    instructions = SHVYABaseInstructions.get()

    assert "MANDATORY ORGANIZATION INFORMATION ALIGNMENT" in instructions
    assert "organization.about" in instructions
    assert "organization.bot_languages" in instructions
    assert "organization.qualification_requirements" in instructions
    assert "organization.engagement_instructions" in instructions
    assert "authoritative business configuration" in instructions
    assert "Unknown, unanswered, assumed, or merely implied criteria" in instructions
    assert "Never request a transition to the Qualified stage" in instructions
    assert "mandatory on EVERY customer-facing turn" in instructions


def test_engagement_input_carries_every_organization_information_value():
    context = build_context()
    payload = json.loads(EngagementService()._build_input(context=context))
    organization = payload["organization"]

    assert organization["about"] == context.organization["about"]
    assert organization["bot_languages"] == context.organization["bot_languages"]
    assert (
        organization["qualification_requirements"]
        == context.organization["qualification_requirements"]
    )
    assert (
        organization["engagement_instructions"]
        == context.organization["engagement_instructions"]
    )


def test_engagement_system_prompt_enforces_org_rules_before_task_instructions():
    context = build_context()
    instructions = EngagementService()._build_instructions(context=context)

    alignment_marker = "MANDATORY ORGANIZATION INFORMATION ALIGNMENT"
    task_marker = "SHVYA AI ENGAGEMENT TASK"

    assert alignment_marker in instructions
    assert context.organization["engagement_instructions"] in instructions
    assert instructions.index(alignment_marker) < instructions.index(task_marker)
    assert "MUST use a configured language" in instructions
    assert "conversation is primary evidence" in instructions
    assert "It does NOT make the lead authoritative" in instructions
