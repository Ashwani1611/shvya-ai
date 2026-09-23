"""Backend-authored qualification response planning."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .common import _norm


def _render_requirement(requirement: dict[str, Any] | None) -> str:
    if not isinstance(requirement, dict):
        return ""
    question = str(
        requirement.get("question") or requirement.get("label") or ""
    ).strip()
    base = question.splitlines()[0].strip()
    options = [
        item
        for item in requirement.get("options") or []
        if isinstance(item, dict) and str(item.get("value") or "").strip()
    ]
    if not options:
        return question
    return base + "\n" + "\n".join(
        f"{str(item.get('key') or '').strip()}. {str(item.get('value') or '').strip()}"
        for item in options
    )


def _requirement_payload(requirement: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(requirement, dict):
        return None
    return {
        "id": str(requirement.get("id") or ""),
        "question": str(requirement.get("question") or "").splitlines()[0].strip(),
        "options": deepcopy(requirement.get("options") or []),
        "rendered": _render_requirement(requirement),
    }


def _start_plan(requirement: dict[str, Any]) -> dict[str, Any]:
    return {
        "response_type": "qualification_start",
        "qualification_complete": False,
        "acknowledgement_required": False,
        "next_requirement": _requirement_payload(requirement),
        "execution_results": {
            "attributes": [],
            "stage_transition": None,
            "other_actions": [],
        },
    }


def _clarification_plan(
    *,
    source,
    requirement: dict[str, Any],
) -> dict[str, Any]:
    return {
        "response_type": "qualification_clarification",
        "qualification_complete": False,
        "acknowledgement_required": False,
        "clarification_context": {"raw_answer": str(source.body or "")},
        "next_requirement": _requirement_payload(requirement),
        "execution_results": {
            "attributes": [],
            "stage_transition": None,
            "other_actions": [],
        },
    }


def _plan(*, source, answer, state, requirements, config, results) -> dict[str, Any]:
    from apps.ai_engagement.services.qualification_state import next_requirement

    completed = _norm(state.get("qualification_status")) == "completed"
    target = config.get("completion_stage")
    source_id = str(getattr(source, "id", "") or "")
    answered_requirement = next(
        (
            requirement
            for requirement in requirements
            if str(
                (
                    (state.get("requirement_states") or {}).get(
                        str(requirement.get("id") or "")
                    )
                    or {}
                ).get("source_message_id")
                or ""
            )
            == source_id
            and _norm(
                (
                    (state.get("requirement_states") or {}).get(
                        str(requirement.get("id") or "")
                    )
                    or {}
                ).get("status")
            )
            == "answered"
        ),
        None,
    )
    acknowledgement_context = {
        "raw_answer": str(source.body or ""),
        "normalized_answer": answer,
        "requirement_id": (
            str(answered_requirement.get("id") or "")
            if isinstance(answered_requirement, dict)
            else ""
        ),
        "question": (
            str(
                answered_requirement.get("question")
                or answered_requirement.get("label")
                or ""
            )
            .splitlines()[0]
            .strip()
            if isinstance(answered_requirement, dict)
            else ""
        ),
    }
    attribute_results = [
        result
        for result in results
        if isinstance(result, dict) and result.get("type") == "attribute_updates"
    ]
    stage_result = next(
        (
            result
            for result in results
            if isinstance(result, dict) and result.get("type") == "pipeline_transition"
        ),
        None,
    )

    if completed:
        return {
            "response_type": "qualification_complete",
            "qualification_complete": True,
            "acknowledgement_required": True,
            "acknowledgement_context": deepcopy(acknowledgement_context),
            "final_configured_acknowledgement": {"value": config.get("final_ack")},
            "execution_results": {
                "attributes": attribute_results,
                "stage_transition": {
                    "configured": bool(target),
                    "target_stage_id": str(target["id"]) if target else None,
                    "target_pipeline_id": str(target["pipeline_id"]) if target else None,
                    "result": deepcopy(stage_result),
                },
                "other_actions": [
                    result
                    for result in results
                    if isinstance(result, dict)
                    and result.get("type")
                    not in {"attribute_updates", "pipeline_transition"}
                ],
            },
        }

    next_item = next_requirement(
        requirements,
        state.get("requirement_states") or {},
    )
    return {
        "response_type": "qualification_progress",
        "qualification_complete": False,
        "acknowledgement_required": True,
        "acknowledgement_context": deepcopy(acknowledgement_context),
        "next_requirement": _requirement_payload(next_item),
        "execution_results": {
            "attributes": attribute_results,
            "stage_transition": None,
            "other_actions": [
                result
                for result in results
                if isinstance(result, dict)
                and result.get("type") not in {"attribute_updates", "pipeline_transition"}
            ],
        },
    }
