"""Validated qualification evidence and persistence helpers."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from .common import _PLAN_KEY, _norm, _processing, _save_processing


def _verify_attribute(lead, key, expected) -> dict[str, Any]:
    lead.refresh_from_db(fields=["attributes"])
    actual = (lead.attributes or {}).get(key)
    verified = actual == expected
    return {
        "type": "attribute_updates",
        "status": "executed" if verified else "failed",
        "verified": verified,
        "updates": [
            {
                "key": key,
                "expected": expected,
                "actual": actual,
            }
        ],
    }


def _verify_stage(lead, target) -> dict[str, Any]:
    lead.refresh_from_db(fields=["pipeline", "stage"])
    actual_stage_id = str(lead.stage_id or "")
    verified = actual_stage_id == str(target["id"])
    return {
        "type": "pipeline_transition",
        "status": "executed" if verified else "failed",
        "verified": verified,
        "target_stage_id": str(target["id"]),
        "target_pipeline_id": str(target["pipeline_id"]),
        "actual_stage_id": actual_stage_id,
        "actual_pipeline_id": str(lead.pipeline_id or ""),
    }


def _persist_plan_only(
    *,
    lead,
    source,
    plan: dict[str, Any],
    intent: str,
) -> None:
    from apps.ai_engagement.services.canonical_architecture import StateReconciler

    processing = _processing(source)
    processing[_PLAN_KEY] = deepcopy(plan)
    _save_processing(source, processing)

    reconciler = StateReconciler()
    snapshot = reconciler.build(
        lead=lead,
        source_message_id=source.id,
        execution_results=[],
        structured_decision={
            "intent": intent,
            "source_message_id": str(source.id),
            "qualification_updates": [],
            "attribute_updates": [],
            "workflow_actions": [],
        },
    )
    snapshot["response_plan"] = deepcopy(plan)
    reconciler.persist_for_source(
        lead=lead,
        source_message_id=source.id,
        snapshot=snapshot,
    )


def _asked_requirement(*, state, requirements):
    current_id = str(state.get("current_requirement_id") or "").strip()
    last_asked_id = str(state.get("last_asked_requirement_id") or "").strip()
    if not current_id or current_id != last_asked_id:
        return None
    status = _norm(
        ((state.get("requirement_states") or {}).get(current_id) or {}).get("status")
    )
    if status not in {"asked", "unclear"}:
        return None
    return next(
        (
            requirement
            for requirement in requirements
            if str(requirement.get("id") or "") == current_id
        ),
        None,
    )


def _additional_explicit_updates(*, organization, requirements, state, source, active_id):
    """Optional volunteered answers still use the one validated contract.

    The organization must enable multi-answer capture. An unasked option letter
    or unrelated number is never an answer to another requirement. No fuzzy CRM
    mapping and no extra provider call are introduced.
    """
    from apps.ai_engagement.services.sales_intelligence import settings_section
    from apps.ai_engagement.services.intent_rules import question_options

    config = settings_section(organization.settings, "ai_qualification")
    if config.get("capture_multiple_answers", False) is not True:
        return []
    output = []
    text = str(source.body or "").strip()
    for requirement in requirements:
        rid = str(requirement.get("id") or "")
        status = (state.get("requirement_states", {}).get(rid) or {}).get("status")
        if rid == active_id or status in {"answered", "skipped", "not_applicable"}:
            continue
        question = str(requirement.get("question") or "")
        options = question_options(question)
        value = None
        if options:
            matches = [str(item["value"]) for item in options if re.search(
                r"(?<![\w])" + re.escape(str(item["value"])) + r"(?![\w])", text, re.I)]
            if len(matches) == 1 and not re.search(
                r"(?:not|no|nahi|nahin|नहीं)\s+" + re.escape(matches[0]), text, re.I):
                value = matches[0]
        elif re.search(r"\b(?:leads?|enquiries?|inquiries?)\b", question, re.I) and re.search(
                r"\b(?:how many|volume|per day|daily)\b", question, re.I):
            match = re.search(r"(?<!\w)(\d+)\s+(?:leads?|enquiries?|inquiries?)(?:\s+(?:per day|daily|every day))?\b", text, re.I)
            if match:
                value = int(match.group(1))
        if value is not None:
            output.append({"requirement_id": rid, "value": value,
                           "source_message_id": str(source.pk), "evidence": text})
    return output
