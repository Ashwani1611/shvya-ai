from __future__ import annotations

import re
from typing import Any


_SECTION_ALIASES = {
    "qualification_criteria": {
        "qualification criteria",
        "qualification criterion",
        "qualification rules",
        "qualification rule",
        "qualification",
    },
    "stage_shifting": {
        "stage shifting",
        "stage shift",
        "stage movement",
        "stage routing",
        "pipeline shifting",
        "pipeline shift",
        "pipeline routing",
    },
    "attribute_mapped": {
        "attribute mapped",
        "attributes mapped",
        "attribute mapping",
        "attribute mappings",
        "attribute map",
        "attribute filling",
        "attribute fill",
    },
    "reminders": {
        "reminder",
        "reminders",
        "reminder rules",
        "follow up reminder",
        "follow-up reminder",
    },
}

_HEADING_RE = re.compile(r"^\s*#{1,6}\s*(?P<title>.+?)\s*#*\s*$")
_BULLET_RE = re.compile(r"^\s*(?:[-*•]+|\d+[.)])\s*")
_QUESTION_START_RE = re.compile(
    r"^(?:what|which|where|who|how|is|are|do|does|did|have|has|can|could|would|will|select|choose|share|tell)\b",
    flags=re.IGNORECASE,
)
_OPTION_RE = re.compile(r"^\s*(?:[-*•]\s*)?(?:[A-Za-z]|\d{1,2})\s*[\)\].:\-]\s+.+$")


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _canonical_heading(value: str) -> str | None:
    title = _clean(value).strip(" :.-_").casefold()
    for key, aliases in _SECTION_ALIASES.items():
        if title in aliases:
            return key
    return None


def _plain_heading(line: str) -> str | None:
    cleaned = _clean(line).strip(" :")
    if not cleaned:
        return None
    return _canonical_heading(cleaned)


def parse_engagement_instruction_sections(raw: str) -> dict[str, str]:
    """Extract authored AI Brain policy sections without interpreting their prose.

    The UI allows free-form Engagement Instructions, so headings are deliberately
    tolerant of both ``##Stage shifting`` and ``## Stage shifting``.  Unknown
    headings remain part of general instructions and are not treated as CRM policy.
    """

    buckets: dict[str, list[str]] = {key: [] for key in _SECTION_ALIASES}
    current: str | None = None

    for raw_line in str(raw or "").splitlines():
        line = raw_line.rstrip()
        heading = _HEADING_RE.match(line)
        if heading:
            canonical = _canonical_heading(heading.group("title"))
            current = canonical
            continue

        canonical_plain = _plain_heading(line)
        if canonical_plain is not None and len(_clean(line)) <= 40:
            current = canonical_plain
            continue

        if current is not None and line.strip():
            buckets[current].append(line.strip())

    return {
        key: "\n".join(lines).strip()
        for key, lines in buckets.items()
        if lines
    }


def section_lines(raw: str, section: str) -> list[str]:
    value = parse_engagement_instruction_sections(raw).get(section, "")
    result: list[str] = []
    for line in value.splitlines():
        cleaned = _BULLET_RE.sub("", line).strip()
        if cleaned:
            result.append(cleaned)
    return result


def _looks_like_questionnaire(value: str) -> bool:
    lines = [line.strip() for line in str(value or "").splitlines() if line.strip()]
    if not lines:
        return False

    if any("?" in line for line in lines):
        return True
    if any(_QUESTION_START_RE.match(_BULLET_RE.sub("", line).strip()) for line in lines):
        return True
    if sum(1 for line in lines if _OPTION_RE.match(line)) >= 2:
        return True

    compact = _clean(value).casefold()
    return bool(
        re.match(
            r"^(?:identify|collect|capture|ask\s+for|qualify\s+(?:on|using|based\s+on))\b",
            compact,
        )
    )


def effective_qualification_source(
    *,
    qualification_requirements: str,
    engagement_instructions: str,
) -> tuple[str, str]:
    """Return the one questionnaire source used by generation and persistence.

    The dedicated Qualification Requirements field remains the primary source for
    backwards compatibility.  When it is empty, a real questionnaire authored
    under ``##Qualification criteria`` becomes the questionnaire instead.  Pure
    completion/routing prose in that section is never miscompiled as a question.
    """

    dedicated = str(qualification_requirements or "").strip()
    if dedicated:
        return dedicated, "qualification_requirements"

    section = parse_engagement_instruction_sections(engagement_instructions).get(
        "qualification_criteria", ""
    )
    if section and _looks_like_questionnaire(section):
        return section, "engagement_instructions.qualification_criteria"
    return "", "none"


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
        qualification_requirements=getattr(org_info, "qualification_requirements", ""),
        engagement_instructions=getattr(org_info, "engagement_instructions", ""),
    )
