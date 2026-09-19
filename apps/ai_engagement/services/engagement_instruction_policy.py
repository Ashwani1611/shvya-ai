from __future__ import annotations

import re
from typing import Any


_BULLET_RE = re.compile(r"^\s*(?:[-*•]+|\d+[.)])\s*")


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def parse_engagement_instruction_sections(raw: str) -> dict[str, str]:
    from apps.ai_engagement.services.playbook import parse_playbook
    return parse_playbook(raw)


def section_lines(raw: str, section: str) -> list[str]:
    value = parse_engagement_instruction_sections(raw).get(section, "")
    if section in {"stage_shifting", "attribute_mapped", "reminders"}:
        from apps.ai_engagement.services.playbook import policy_blocks
        return policy_blocks(value)
    result: list[str] = []
    for line in value.splitlines():
        cleaned = _BULLET_RE.sub("", line).strip()
        if cleaned:
            result.append(cleaned)
    return result


def effective_qualification_source(*, ai_playbook: str) -> tuple[str, str]:
    from apps.ai_engagement.services.playbook import qualification_questions
    questions = qualification_questions(ai_playbook)
    return questions, "ai_playbook.qualification_questions" if questions else "none"


def compile_engagement_instruction_policy(raw: str) -> dict[str, Any]:
    sections = parse_engagement_instruction_sections(raw)
    qualification_text = sections.get("qualification_criteria", "")
    lowered = _clean(qualification_text).casefold()

    all_answered = any(
        phrase in lowered
        for phrase in (
            "all questions",
            "all qualification questions",
            "all required questions",
            "all requirements",
            "after all",
            "once all",
            "when all",
            "complete all",
        )
    )

    return {
        "qualification_criteria": section_lines(raw, "qualification_criteria"),
        "stage_shifting": section_lines(raw, "stage_shifting"),
        "attribute_mapped": section_lines(raw, "attribute_mapped"),
        "reminders": section_lines(raw, "reminders"),
        "qualification_completion": (
            "all_required_answered" if all_answered else "configured"
        ),
    }


def effective_qualification_source_for_org_info(org_info) -> tuple[str, str]:
    if org_info is None:
        return "", "none"
    return effective_qualification_source(
        ai_playbook=getattr(org_info, "ai_playbook", ""),
    )
