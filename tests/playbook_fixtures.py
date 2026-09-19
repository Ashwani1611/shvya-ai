"""Explicit playbook fixtures for tests of the single authoring contract."""

import re

from apps.ai_engagement.services.playbook import qualification_questions


def with_playbook_section(playbook, section, value):
    pattern = re.compile(r"(?ims)^##\s*" + re.escape(section) + r"\s*\n.*?(?=^##|\Z)")
    replacement = f"##{section}\n{value or ''}\n\n"
    if pattern.search(playbook or ""):
        return pattern.sub(lambda match: replacement, playbook, count=1).strip()
    return ((playbook or "").rstrip() + "\n\n" + replacement).strip()


def build_ai_playbook(*, questions="", rules="", criteria="All required questions are answered."):
    # Empty fixtures represent an unconfigured organization, as before.
    if not questions and not rules:
        return ""
    text = f"##Rules\n{rules}\n\n##Qualification Questions\n{questions}".strip()
    if criteria and not re.search(r"(?im)^#+\s*Qualification Criteria\b", rules):
        text += f"\n\n##Qualification Criteria\n{criteria}"
    return text


def replace_playbook_questions(playbook, questions):
    updated = with_playbook_section(playbook, "Qualification Questions", questions)
    if questions and not re.search(r"(?im)^#+\s*Qualification Criteria\b", updated):
        updated = with_playbook_section(updated, "Qualification Criteria", "All required questions are answered.")
    return updated


__all__ = ["build_ai_playbook", "with_playbook_section", "replace_playbook_questions", "qualification_questions"]
