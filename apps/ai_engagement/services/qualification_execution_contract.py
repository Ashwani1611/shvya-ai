from __future__ import annotations

import json
import logging
import re
from datetime import timedelta
from copy import deepcopy
from dataclasses import replace
from functools import wraps
from typing import Any

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)
_INSTALLED = False
_CONTRACT_KEY = "qualification_execution_contract"
_PLAN_KEY = "qualification_response_plan"

_ACK_LABEL = re.compile(
    r"^\s*(?:[-*•]\s*)?(?P<label>(?:final\s+)?(?:acknowledg(?:e)?ment|completion)\s+message|final\s+acknowledg(?:e)?ment)\s*(?::|=|->|→)\s*(?P<value>.+?)\s*$",
    re.I,
)
_ACK_HEADING_ONLY = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:final\s+)?(?:acknowledg(?:e)?ment|completion)\s+message\s*:?\s*$",
    re.I,
)
_QUESTION_FRAGMENT_RE = re.compile(
    r"^(?:what|which|where|who|how|is|are|do|does|did|have|has|can|could|would|will|"
    r"tell|share|select|choose)\b",
    re.I,
)
_LABEL_ONLY = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:(?:final\s+)?(?:acknowledg(?:e)?ment|completion)\s+message|final\s+acknowledg(?:e)?ment|qualification\s+requirements?|attribute\s+mapped|stage\s+shifting)\s*:?\s*$",
    re.I,
)
_COMPLETION_RULE = re.compile(
    r"\b(?:qualification\s+(?:is\s+)?(?:complete|completed)|(?:all|every)\s+(?:required\s+)?(?:qualification\s+)?(?:questions?|requirements?|answers?)\s+(?:are\s+)?(?:answered|complete|completed)|(?:after|once|when)\s+(?:all|every)\s+(?:required\s+)?(?:qualification\s+)?(?:questions?|requirements?)\s+(?:are\s+)?(?:answered|complete|completed))\b",
    re.I,
)
_GENERIC_ACKS = {
    "got it",
    "great",
    "nice",
    "okay",
    "ok",
    "noted",
    "thanks",
    "thank you",
}
_GREETING_RE = re.compile(r"^[\s*_]*(?:hi|hello|hey|welcome)\b", re.I)

_PLAN_INSTRUCTIONS = """
BACKEND RESPONSE PLAN CONTRACT
- response_plan is backend-authoritative when present.
- qualification_start: output the backend-authored configured requirement/options; do not invent or reorder them.
- qualification_progress: write ONE short personalized acknowledgement reflecting acknowledgement_context. Do not rewrite the next question/options; backend appends them.
- qualification_clarification: briefly ask for clarification without guessing. Backend appends the active configured requirement/options.
- qualification_complete: write ONE short personalized acknowledgement reflecting the final answer. Backend appends the configured final acknowledgement VALUE.
- Never expose configuration labels, requirement ids, attribute keys, stage ids, workflow ids, or execution metadata.
- Never claim an attribute/stage/workflow action succeeded unless reconciled backend state confirms it.
""".strip()

_MAPPING_ERROR_CODES = {
    "invalid_attribute_mapping_rule",
    "unknown_requirement_mapping_reference",
    "unknown_attribute_mapping_reference",
    "ambiguous_attribute_mapping",
}


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _norm(value: Any) -> str:
    return _clean(value).casefold()


def _strip_quotes(value: str) -> str:
    value = str(value or "").strip()
    if (
        len(value) > 1
        and value[0] == value[-1]
        and value[0] in {"'", '"'}
    ):
        return value[1:-1].strip()
    return value


def _processing(message) -> dict[str, Any]:
    payload = message.raw_payload if message and isinstance(message.raw_payload, dict) else {}
    data = payload.get("shvya_ai_processing")
    return deepcopy(data) if isinstance(data, dict) else {}


def _save_processing(message, processing: dict[str, Any]) -> None:
    payload = deepcopy(message.raw_payload) if isinstance(message.raw_payload, dict) else {}
    payload["shvya_ai_processing"] = deepcopy(processing)
    message.raw_payload = payload
    message.save(update_fields=["raw_payload", "updated_at"])


def _reference(value: Any) -> str:
    text = _norm(value).strip("`'\"[](){} ")
    return re.sub(
        r"^(?:requirement|question|attribute|field)\s+",
        "",
        text,
    ).strip()


def _requirement_ref(
    value: str,
    requirements: list[dict[str, Any]],
) -> dict[str, Any] | None:
    ref = _reference(value)
    match = re.fullmatch(r"q(?:uestion)?\s*(\d+)", ref)
    if match:
        found = [
            requirement
            for requirement in requirements
            if int(requirement.get("priority") or 0) == int(match.group(1))
        ]
        return found[0] if len(found) == 1 else None

    found = []
    for requirement in requirements:
        aliases = {
            _reference(requirement.get("id")),
            _reference(requirement.get("stable_id")),
            _reference(requirement.get("label")),
            _reference(str(requirement.get("question") or "").splitlines()[0]),
            *(_reference(item) for item in requirement.get("legacy_ids") or []),
        }
        aliases.discard("")
        if ref and ref in aliases:
            found.append(requirement)
    return found[0] if len(found) == 1 else None


def _attribute_ref(
    value: str,
    definitions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    ref = _reference(value)
    found = [
        item
        for item in definitions
        if ref
        and ref
        in {
            _reference(item.get("key")),
            _reference(item.get("name")),
        }
    ]
    return found[0] if len(found) == 1 else None


def _attribute_refs(
    value: str,
    definitions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Resolve authored attribute targets without dropping valid siblings.

    Exact names/keys remain authoritative. Multi-target shorthand is supported
    only when the full right-hand side is not itself an attribute name, so an
    attribute such as "Leads/d" remains one field while authored forms such as
    "Lead Management Tool (+ Using Whatsapp / CRM)" can resolve several fields.

    A missing secondary target must not cancel the valid primary/other targets.
    Missing names are returned separately so configuration diagnostics remain
    visible instead of silently losing all CRM writes for the requirement.
    """
    direct = _attribute_ref(value, definitions)
    if direct is not None:
        return [direct], []

    text = str(value or "").strip()
    text = re.sub(r"\(\s*\+", "+", text)
    text = text.replace(")", " ")
    parts = [
        part.strip()
        for part in re.split(r"\s*(?:\+|/|,|\band\b)\s*", text, flags=re.I)
        if part.strip()
    ]
    if len(parts) < 2:
        return [], ([text] if text else [])

    resolved: list[dict[str, Any]] = []
    unresolved: list[str] = []
    seen: set[str] = set()
    for part in parts:
        item = _attribute_ref(part, definitions)
        if item is None:
            unresolved.append(part)
            continue
        key = str(item.get("key") or "")
        if key and key not in seen:
            resolved.append(item)
            seen.add(key)
    return resolved, unresolved


def _split_mapping(line: str) -> tuple[str, str] | None:
    text = str(line or "").strip()
    parts = re.split(r"\s*(?:->|=>|→)\s*", text, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    match = re.match(r"^\s*map\s+(.+?)\s+to\s+(.+?)\s*$", text, re.I)
    if match:
        return match.group(1), match.group(2)
    match = re.match(r"^\s*(.+?)\s+maps?\s+to\s+(.+?)\s*$", text, re.I)
    return (match.group(1), match.group(2)) if match else None


def _config(*, organization, requirements: list[dict[str, Any]]) -> dict[str, Any]:
    """Compile deterministic qualification execution configuration for one org."""
    from apps.ai_engagement.models import OrgInfo
    from apps.ai_engagement.services.engagement_instruction_policy import section_lines
    from apps.crm.models import AttributeDefinition, Stage

    info = OrgInfo.objects.filter(organization=organization).first()
    engagement_raw = str(getattr(info, "engagement_instructions", "") or "")
    qualification_raw = str(getattr(info, "qualification_requirements", "") or "")
    raw = engagement_raw
    definitions = list(
        AttributeDefinition.objects.filter(organization=organization).values(
            "key",
            "name",
            "field_type",
            "description",
            "options",
        )
    )

    mappings: dict[str, str] = {}
    mapping_targets: dict[str, list[str]] = {}
    errors: list[dict[str, str]] = []

    mapping_lines = list(section_lines(engagement_raw, "attribute_mapped"))
    # Attribute mappings are configuration, not qualification prose. Accept
    # explicit mapping-shaped lines from either AI Setup field so organizations
    # do not silently lose CRM writes when they keep Q1 -> Attribute next to the
    # questionnaire. Only lines that parse as mappings are admitted here.
    for raw_line in qualification_raw.splitlines():
        cleaned = raw_line.strip().lstrip("-*• ").strip()
        if cleaned and _split_mapping(cleaned):
            mapping_lines.append(cleaned)
    mapping_lines = list(dict.fromkeys(mapping_lines))

    for line in mapping_lines:
        pair = _split_mapping(line)
        if not pair:
            errors.append(
                {
                    "type": "configuration_error",
                    "status": "failed",
                    "code": "invalid_attribute_mapping_rule",
                    "detail": line,
                }
            )
            continue
        requirement = _requirement_ref(pair[0], requirements)
        attributes, unresolved_attributes = _attribute_refs(pair[1], definitions)
        if requirement is None:
            errors.append(
                {
                    "type": "configuration_error",
                    "status": "failed",
                    "code": "unknown_requirement_mapping_reference",
                    "detail": pair[0],
                }
            )
            continue
        if unresolved_attributes:
            for unresolved in unresolved_attributes:
                errors.append(
                    {
                        "type": "configuration_error",
                        "status": "failed",
                        "code": "unknown_attribute_mapping_reference",
                        "detail": unresolved,
                    }
                )
        if not attributes:
            continue
        from apps.ai_engagement.services.confidentiality import (
            is_sensitive_attribute_definition,
        )

        sensitive = [
            attribute
            for attribute in attributes
            if is_sensitive_attribute_definition(attribute)
        ]
        if sensitive:
            errors.append(
                {
                    "type": "configuration_error",
                    "status": "failed",
                    "code": "sensitive_attribute_mapping",
                    "detail": pair[1],
                }
            )
            continue
        requirement_id = str(requirement.get("id") or "")
        targets = mapping_targets.setdefault(requirement_id, [])
        for attribute in attributes:
            attribute_key = str(attribute.get("key") or "")
            if attribute_key and attribute_key not in targets:
                targets.append(attribute_key)
            # Keep the first mapping as the backwards-compatible primary mapping
            # for older reconciliation callers. Qualification execution itself
            # uses the full one-to-many mapping_targets list.
            if attribute_key:
                mappings.setdefault(requirement_id, attribute_key)

    acknowledgement_values: list[str] = []
    for source_text in (engagement_raw, qualification_raw):
        source_lines = source_text.splitlines()
        for index, line in enumerate(source_lines):
            stripped = line.strip()
            match = _ACK_LABEL.match(stripped)
            if match:
                value = _strip_quotes(match.group("value"))
                if value and not _LABEL_ONLY.match(value):
                    acknowledgement_values.append(value)
                else:
                    errors.append(
                        {
                            "type": "configuration_error",
                            "status": "failed",
                            "code": "invalid_final_acknowledgement_value",
                            "detail": match.group("label"),
                        }
                    )
                continue

            if not _ACK_HEADING_ONLY.match(stripped):
                continue
            # Also support the natural two-line UI format:
            # Acknowledgment Message:
            # "Thanks for sharing the details..."
            value = ""
            for following in source_lines[index + 1:]:
                candidate = following.strip()
                if not candidate:
                    continue
                if _LABEL_ONLY.match(candidate):
                    break
                value = _strip_quotes(candidate.lstrip("-*• ").strip())
                break
            if value:
                acknowledgement_values.append(value)
            else:
                errors.append(
                    {
                        "type": "configuration_error",
                        "status": "failed",
                        "code": "invalid_final_acknowledgement_value",
                        "detail": stripped.rstrip(":"),
                    }
                )
    acknowledgement_values = list(dict.fromkeys(acknowledgement_values))
    final_ack = acknowledgement_values[0] if len(acknowledgement_values) == 1 else None
    if len(acknowledgement_values) > 1:
        errors.append(
            {
                "type": "configuration_error",
                "status": "failed",
                "code": "ambiguous_final_acknowledgement",
                "detail": "Multiple values configured.",
            }
        )

    stages = list(
        Stage.objects.filter(
            pipeline__organization=organization,
            pipeline__is_active=True,
            is_active=True,
        ).values("id", "name", "pipeline_id", "pipeline__name")
    )
    stage_targets = []
    for line in section_lines(raw, "stage_shifting"):
        if not _COMPLETION_RULE.search(line):
            continue
        normalized = _norm(line)
        matches = [
            stage
            for stage in stages
            if _norm(stage["name"])
            and re.search(
                rf"(?<![a-z0-9]){re.escape(_norm(stage['name']))}(?![a-z0-9])",
                normalized,
            )
        ]
        if len(matches) > 1:
            matches = [
                stage
                for stage in matches
                if _norm(stage["pipeline__name"])
                and _norm(stage["pipeline__name"]) in normalized
            ]
        if len(matches) == 1:
            stage_targets.append(matches[0])
        else:
            errors.append(
                {
                    "type": "configuration_error",
                    "status": "failed",
                    "code": "unresolved_completion_stage_rule",
                    "detail": line,
                }
            )
    unique_targets = {str(stage["id"]): stage for stage in stage_targets}
    completion_stage = (
        next(iter(unique_targets.values())) if len(unique_targets) == 1 else None
    )
    if len(unique_targets) > 1:
        errors.append(
            {
                "type": "configuration_error",
                "status": "failed",
                "code": "ambiguous_completion_stage_rule",
                "detail": "Multiple completion targets configured.",
            }
        )

    return {
        "mappings": mappings,
        "mapping_targets": mapping_targets,
        "final_ack": final_ack,
        "completion_stage": completion_stage,
        "reminder_rules": section_lines(engagement_raw, "reminders"),
        "errors": errors,
    }


def _mapping_keys(config: dict[str, Any], requirement_id: str) -> list[str]:
    targets = (config.get("mapping_targets") or {}).get(str(requirement_id))
    if isinstance(targets, list):
        return [str(item).strip() for item in targets if str(item or "").strip()]
    primary = str((config.get("mappings") or {}).get(str(requirement_id)) or "").strip()
    return [primary] if primary else []


def _completion_target(*, lead, state: dict[str, Any], config: dict[str, Any]):
    """Resolve the qualification-completion stage deterministically.

    An explicit Stage Shifting completion rule wins. Otherwise use the
    qualification state's validated same-pipeline Qualified stage. If a pipeline
    has no uniquely named Qualified stage, advance to the next active stage by
    display order so a completed qualification is never left in New Lead.
    """
    target = config.get("completion_stage")
    if isinstance(target, dict) and target.get("id") is not None:
        return target

    from apps.crm.models import Stage

    stage_id = str(state.get("qualified_stage_id") or "").strip()
    if stage_id:
        target = (
            Stage.objects.filter(
                id=stage_id,
                pipeline__organization=lead.organization,
                pipeline__is_active=True,
                is_active=True,
            )
            .values("id", "name", "pipeline_id", "pipeline__name", "display_order")
            .first()
        )
        if target is not None:
            return target

    pipeline_id = getattr(lead, "pipeline_id", None)
    if not pipeline_id:
        return None
    candidates = list(
        Stage.objects.filter(
            pipeline_id=pipeline_id,
            pipeline__organization=lead.organization,
            pipeline__is_active=True,
            is_active=True,
        )
        .order_by("display_order", "name", "id")
        .values("id", "name", "pipeline_id", "pipeline__name", "display_order")
    )
    qualified = [
        item
        for item in candidates
        if _norm(item.get("name")) == "qualified"
    ]
    if len(qualified) == 1:
        return qualified[0]

    current_stage_id = str(getattr(lead, "stage_id", "") or "")
    current = next(
        (item for item in candidates if str(item.get("id") or "") == current_stage_id),
        None,
    )
    if current is None:
        return None
    current_order = int(current.get("display_order") or 0)
    following = [
        item
        for item in candidates
        if str(item.get("id") or "") != current_stage_id
        and int(item.get("display_order") or 0) > current_order
    ]
    return following[0] if following else None


_COMPLETION_REMINDER_SCOPE_RE = re.compile(
    r"\b(?:qualification\s+(?:is\s+)?(?:complete|completed)|"
    r"after\s+(?:all|every)\s+(?:required\s+)?(?:qualification\s+)?"
    r"(?:questions?|requirements?|answers?)\s+(?:are\s+)?(?:answered|complete|completed)|"
    r"when\s+(?:all|every)\s+(?:required\s+)?(?:qualification\s+)?"
    r"(?:questions?|requirements?|answers?)\s+(?:are\s+)?(?:answered|complete|completed))\b",
    re.I,
)
_REMINDER_CREATE_RE = re.compile(
    r"\b(?:create|set|add|schedule)\b.{0,40}\breminder\b|"
    r"\breminder\b.{0,40}\b(?:create|set|add|schedule)\b",
    re.I,
)
_REMINDER_RELATIVE_RE = re.compile(
    r"\b(?:in|after)\s+(?P<amount>\d{1,3})\s*"
    r"(?P<unit>minutes?|mins?|hours?|hrs?|days?|weeks?)\b",
    re.I,
)


def _configured_completion_reminders(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Build one deterministic follow-up reminder when qualification completes.

    A valid organization-authored completion reminder keeps its configured due
    time. If no usable completion rule exists, create the standard SHVYA
    follow-up reminder for 24 hours later so a fully qualified lead cannot finish
    the questionnaire without a next human action.
    """
    from apps.ai_engagement.services.reminder_time_runtime import parse_grounded_due_at

    actions: list[dict[str, Any]] = []
    for rule in config.get("reminder_rules") or []:
        text = str(rule or "").strip()
        if not text or not _COMPLETION_REMINDER_SCOPE_RE.search(text):
            continue
        if not _REMINDER_CREATE_RE.search(text):
            continue

        due_at = parse_grounded_due_at(text)
        if due_at is None:
            relative = _REMINDER_RELATIVE_RE.search(text)
            if relative:
                amount = int(relative.group("amount"))
                unit = relative.group("unit").casefold()
                if unit.startswith(("min", "minute")):
                    delta = timedelta(minutes=amount)
                elif unit.startswith(("hr", "hour")):
                    delta = timedelta(hours=amount)
                elif unit.startswith("week"):
                    delta = timedelta(weeks=amount)
                else:
                    delta = timedelta(days=amount)
                due_at = (timezone.now() + delta).isoformat()
        if due_at is None:
            continue

        actions.append(
            {
                "type": "create_reminder",
                "title": "Follow up with qualified lead",
                "description": "Configured follow-up reminder after qualification completion.",
                "due_at": due_at,
            }
        )
        break

    if actions:
        return actions

    return [
        {
            "type": "create_reminder",
            "title": "Follow up with qualified lead",
            "description": "Automatic follow-up after qualification completion.",
            "due_at": (timezone.now() + timedelta(days=1)).isoformat(),
        }
    ]


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


def resolve_before_generation(
    *,
    organization,
    lead,
    source_message_id,
    account_id=None,
) -> dict[str, Any]:
    """Resolve deterministic qualification work before customer response generation."""
    from apps.ai_engagement.services import qualification_state as qs
    from apps.ai_engagement.services.canonical_architecture import StateReconciler
    from apps.ai_engagement.services.crm_executor import CRMActionExecutor
    from apps.ai_engagement.services.transactional_turn_runtime import (
        _mark_state_resolved,
        _persist_updates_against_requirements,
        _requirements_for_turn,
    )
    from apps.channels.models import WhatsAppMessage
    from apps.crm.models import Lead

    with transaction.atomic():
        locked = (
            Lead.objects.select_for_update()
            .select_related("organization", "pipeline", "stage")
            .get(pk=lead.pk, organization=organization)
        )
        query = WhatsAppMessage.objects.select_for_update().filter(
            pk=source_message_id,
            organization=organization,
            lead=locked,
            direction=WhatsAppMessage.Direction.INBOUND,
        )
        if account_id is not None:
            query = query.filter(account_id=account_id)
        source = query.first()
        if source is None:
            return {"applied": False, "reason": "source_message_not_found"}

        processing = _processing(source)
        if (
            isinstance(processing.get(_CONTRACT_KEY), dict)
            and processing[_CONTRACT_KEY].get("applied")
        ):
            return {"applied": False, "reason": "already_applied"}

        requirements = _requirements_for_turn(
            organization=organization,
            lead=locked,
        )
        state = qs.state_for_lead(locked, requirements=requirements)
        if not requirements:
            return {"applied": False, "reason": "no_qualification_requirements"}
        if _norm(state.get("qualification_status")) == "completed":
            return {"applied": False, "reason": "qualification_already_complete"}

        requirement = _asked_requirement(
            state=state,
            requirements=requirements,
        )
        if requirement is None:
            next_item = qs.next_requirement(
                requirements,
                state.get("requirement_states") or {},
            )
            if not isinstance(next_item, dict):
                return {"applied": False, "reason": "no_active_requirement"}
            plan = _start_plan(next_item)
            _persist_plan_only(
                lead=locked,
                source=source,
                plan=plan,
                intent="qualification_start",
            )
            return {
                "applied": False,
                "reason": "qualification_start",
                "response_plan": plan,
            }

        active_id = str(requirement.get("id") or "")
        classified = qs._classify_direct_reply(
            text=source.body,
            question=str(requirement.get("question") or requirement.get("label") or ""),
        )
        if classified is None:
            return {"applied": False, "reason": "requires_llm_interpretation"}

        answer_status, answer, confidence = classified
        if answer_status != qs.REQUIREMENT_ANSWERED:
            plan = _clarification_plan(
                source=source,
                requirement=requirement,
            )
            _persist_plan_only(
                lead=locked,
                source=source,
                plan=plan,
                intent="qualification_clarification",
            )
            return {
                "applied": False,
                "reason": "qualification_clarification",
                "response_plan": plan,
            }

        update = {
            "requirement_id": active_id,
            "value": answer,
            "source_message_id": str(source.id),
            "evidence": str(source.body or "").strip(),
        }
        updates = [update, *_additional_explicit_updates(
            organization=organization, requirements=requirements, state=state,
            source=source, active_id=active_id)]
        projected = qs.project_answer_updates(
            state=state,
            requirements=requirements,
            updates=updates,
            messages=[
                {
                    "id": str(source.id),
                    "body": source.body,
                    "direction": "inbound",
                }
            ],
        )
        config = _config(
            organization=organization,
            requirements=requirements,
        )

        # Build the complete authoritative execution plan before mutating state.
        mapped_updates = []
        for item in updates:
            for attribute_key in _mapping_keys(config, item["requirement_id"]):
                mapped_updates.append(
                    {"key": attribute_key, "value": item["value"]}
                )
        # A repeated authored mapping to the same key is harmless; keep only the
        # final value for that key in this turn.
        mapped_updates = list({
            str(item["key"]): item
            for item in mapped_updates
            if str(item.get("key") or "").strip()
        }.values())
        attribute_action = ({"type": "attribute_updates", "updates": mapped_updates}
                            if mapped_updates else None)
        completion_reached = _norm(projected.get("qualification_status")) == "completed"
        target = (
            _completion_target(lead=locked, state=projected, config=config)
            if completion_reached
            else None
        )
        runtime_config = {**config, "completion_stage": target}
        stage_action = (
            {
                "type": "pipeline_transition",
                "stage_shift": {"stage_id": str(target["id"])},
            }
            if completion_reached and isinstance(target, dict)
            else None
        )
        reminder_actions = (
            _configured_completion_reminders(runtime_config)
            if completion_reached
            else []
        )
        execution_plan = {
            "qualification_update": deepcopy(update),
            "qualification_updates": deepcopy(updates),
            "attribute_action": deepcopy(attribute_action),
            "stage_action": deepcopy(stage_action),
            "reminder_actions": deepcopy(reminder_actions),
        }

        results = deepcopy(config["errors"])
        action_types = ["qualification_state"]

        _persist_updates_against_requirements(
            lead=locked,
            requirements=requirements,
            updates=updates,
        )
        locked.refresh_from_db(fields=["attributes", "pipeline", "stage"])

        if attribute_action:
            try:
                CRMActionExecutor().execute(
                    organization=organization,
                    lead=locked,
                    actions=[attribute_action],
                )
                for mapped in mapped_updates:
                    verified_attribute = _verify_attribute(locked, mapped["key"], mapped["value"])
                    results.append(verified_attribute)
                if all(item.get("verified") for item in results if item.get("type") == "attribute_updates"):
                    action_types.append("attribute_updates")
            except Exception as exc:
                results.append(
                    {
                        "type": "attribute_updates",
                        "status": "failed",
                        "verified": False,
                        "code": "attribute_persistence_failed",
                        "detail": str(exc)[:500],
                        "updates": [{"key": item["key"], "expected": item["value"], "actual": None}
                                    for item in mapped_updates],
                    }
                )

        # Stage completion is an independent configured downstream action. A
        # failed/missing mapped attribute is reported, but does not silently erase
        # a separately configured completion-stage action.
        if stage_action:
            try:
                CRMActionExecutor().execute(
                    organization=organization,
                    lead=locked,
                    actions=[stage_action],
                )
                verified_stage = _verify_stage(locked, target)
                results.append(verified_stage)
                if verified_stage["verified"]:
                    action_types.append("pipeline_transition")
            except Exception as exc:
                locked.refresh_from_db(fields=["pipeline", "stage"])
                results.append(
                    {
                        "type": "pipeline_transition",
                        "status": "failed",
                        "verified": False,
                        "code": "stage_transition_failed",
                        "detail": str(exc)[:500],
                        "target_stage_id": str(target["id"]),
                        "target_pipeline_id": str(target["pipeline_id"]),
                        "actual_stage_id": str(locked.stage_id or ""),
                        "actual_pipeline_id": str(locked.pipeline_id or ""),
                    }
                )

        # Completion reminders are created only from explicit authored reminder
        # rules with a resolvable due time. Qualification completion alone never
        # invents a reminder.
        for reminder_action in reminder_actions:
            try:
                reminder_result = CRMActionExecutor().execute(
                    organization=organization,
                    lead=locked,
                    actions=[reminder_action],
                )
                results.extend(reminder_result)
                if reminder_result:
                    action_types.append("create_reminder")
            except Exception as exc:
                results.append(
                    {
                        "type": "create_reminder",
                        "status": "failed",
                        "verified": False,
                        "code": "reminder_creation_failed",
                        "detail": str(exc)[:500],
                    }
                )

        locked.refresh_from_db(fields=["attributes", "pipeline", "stage"])
        final_state = qs.state_for_lead(
            locked,
            requirements=requirements,
        )
        plan = _plan(
            source=source,
            answer=answer,
            state=final_state,
            requirements=requirements,
            config=runtime_config,
            results=results,
        )

        _mark_state_resolved(
            lead=locked,
            inbound=source,
            action_types=action_types,
        )
        source.refresh_from_db(fields=["raw_payload"])
        processing = _processing(source)
        processing[_CONTRACT_KEY] = {
            "applied": True,
            "applied_at": timezone.now().isoformat(),
            "requirement_id": active_id,
            "normalized_answer": answer,
            "confidence": confidence,
            "execution_plan": execution_plan,
            "configuration_errors": deepcopy(config["errors"]),
        }
        processing[_PLAN_KEY] = deepcopy(plan)
        _save_processing(source, processing)

        reconciler = StateReconciler()
        snapshot = reconciler.build(
            lead=locked,
            source_message_id=source.id,
            execution_results=results,
            structured_decision={
                "intent": "qualification_answer",
                "source_message_id": str(source.id),
                "qualification_updates": deepcopy(updates),
                "attribute_updates": (
                    deepcopy(attribute_action["updates"])
                    if attribute_action
                    else []
                ),
                "workflow_actions": [
                    *([deepcopy(stage_action)] if stage_action else []),
                    *deepcopy(reminder_actions),
                ],
            },
        )
        snapshot["response_plan"] = deepcopy(plan)
        reconciler.persist_for_source(
            lead=locked,
            source_message_id=source.id,
            snapshot=snapshot,
        )
        return {
            "applied": True,
            "qualification_status": final_state.get("qualification_status"),
            "response_plan": plan,
            "execution_results": results,
            "stage_id": str(locked.stage_id or ""),
        }


def _plan_from_reconciled(*, lead, source_message_id, snapshot):
    """Build the final response plan from actual persisted state after model interpretation."""
    from apps.ai_engagement.services import qualification_state as qs
    from apps.ai_engagement.services.transactional_turn_runtime import _requirements_for_turn

    requirements = _requirements_for_turn(
        organization=lead.organization,
        lead=lead,
    )
    source = (
        lead.whatsapp_messages.filter(
            pk=source_message_id,
            direction="inbound",
        )
        .only("body")
        .first()
    )
    if source is None or not requirements:
        return None

    state = qs.state_for_lead(lead, requirements=requirements)
    answered = [
        (requirement_id, item)
        for requirement_id, item in (state.get("requirement_states") or {}).items()
        if isinstance(item, dict)
        and str(item.get("source_message_id") or "") == str(source_message_id)
        and _norm(item.get("status")) == "answered"
    ]
    if not answered:
        return None

    config = _config(
        organization=lead.organization,
        requirements=requirements,
    )
    results = [
        deepcopy(item)
        for item in snapshot.get("execution_results") or []
        if isinstance(item, dict)
    ]

    # Verify every explicitly mapped attribute target by DB readback for every
    # answer persisted by this inbound message, regardless of model proposals.
    for requirement_id, item in answered:
        for attribute_key in _mapping_keys(config, str(requirement_id)):
            verification = _verify_attribute(
                lead,
                attribute_key,
                item.get("value"),
            )
            results = [
                result
                for result in results
                if not (
                    result.get("type") == "attribute_updates"
                    and any(
                        str(update.get("key") or "") == attribute_key
                        for update in result.get("updates") or []
                        if isinstance(update, dict)
                    )
                )
            ]
            results.append(verification)

    target = _completion_target(lead=lead, state=state, config=config)
    runtime_config = {**config, "completion_stage": target}
    if target and _norm(state.get("qualification_status")) == "completed":
        verification = _verify_stage(lead, target)
        results = [
            result
            for result in results
            if result.get("type") != "pipeline_transition"
        ]
        results.append(verification)

    return _plan(
        source=source,
        answer=answered[-1][1].get("value"),
        state=state,
        requirements=requirements,
        config=runtime_config,
        results=results,
    )


def _fallback_acknowledgement(plan: dict[str, Any]) -> str:
    """Build a short answer-aware acknowledgement from backend-owned facts."""
    context = plan.get("acknowledgement_context")
    context = context if isinstance(context, dict) else {}
    answer = context.get("normalized_answer")
    if isinstance(answer, bool):
        answer_text = "Yes" if answer else "No"
    else:
        answer_text = _clean(answer or context.get("raw_answer"))
    answer_text = answer_text.strip(" .!?")
    question = _norm(context.get("question"))

    if not answer_text:
        return "Thanks — that helps me understand your setup better."
    if len(answer_text) > 100 or "\n" in answer_text:
        return "Thanks — that helps me understand your setup better."

    answer_norm = _norm(answer_text)
    if "challenge" in question or "problem" in question:
        return f"Got it — {answer_text} sounds like the main challenge right now."
    if (
        ("manage" in question and "lead" in question)
        or "lead-management" in question
        or "lead management" in question
    ):
        return f"Understood — you're currently managing leads through {answer_text}."
    if (
        ("how many" in question and ("lead" in question or "enquir" in question))
        or ("lead" in question and ("per day" in question or "daily" in question))
    ):
        return f"Thanks — {answer_text} gives me a clear picture of your daily lead volume."
    if "run ads" in question or "running ads" in question:
        if answer_norm == "yes":
            return "Got it — you're currently running ads."
        if answer_norm == "no":
            return "Understood — you're not currently running ads."
        return "Got it — that helps me understand your current ad activity."
    if ("lead" in question and "come from" in question) or "lead source" in question:
        label = "sources" if any(token in answer_text for token in (";", ",", " and ")) else "source"
        return f"Thanks — I've noted {answer_text} as your main lead {label}."
    if "type of business" in question or "industry" in question:
        return f"Got it — {answer_text} gives me useful context about your business."
    if "sales team" in question or "salespeople" in question:
        return f"Thanks — {answer_text} gives me a clear picture of your sales team size."
    if "budget" in question:
        return f"Got it — {answer_text} gives me useful budget context."
    if "main goal" in question or ("goal" in question and "shvya" in question):
        return f"That makes sense — {answer_text} gives me a clear picture of what you want to improve."
    if ("crm" in question or "lead-management software" in question) and (
        answer_norm in {"yes", "no"}
    ):
        return "Thanks — that clarifies your current CRM setup."
    if "how soon" in question or "implement" in question:
        return f"Understood — {answer_text} is your implementation timeline."
    if "purchase decision" in question or "final purchase" in question:
        return "Thanks — that clarifies how the purchase decision works on your side."
    if "whatsapp setup" in question:
        return f"Got it — {answer_text} is your current WhatsApp setup."
    if "customer conversations" in question and (
        "month" in question or "monthly" in question
    ):
        return f"Thanks — {answer_text} gives me a clear picture of your monthly conversation volume."
    if answer_norm in {"yes", "no"}:
        return "Got it — thanks for confirming that."
    return f"Thanks — I've noted {answer_text}. That gives me useful context."


def _ack_from_message(message: str, plan: dict[str, Any]) -> str:
    text = str(message or "").replace("\\n", "\n").strip()
    final_value = str(
        ((plan.get("final_configured_acknowledgement") or {}).get("value") or "")
    ).strip()
    next_rendered = str(
        ((plan.get("next_requirement") or {}).get("rendered") or "")
    ).strip()
    if final_value:
        text = text.replace(final_value, " ")
    if next_rendered:
        text = text.replace(next_rendered, " ")

    next_question = _norm((plan.get("next_requirement") or {}).get("question"))
    option_values = {
        _norm(item.get("value"))
        for item in ((plan.get("next_requirement") or {}).get("options") or [])
        if isinstance(item, dict)
    }
    kept = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _ACK_LABEL.match(line)
        if match:
            line = _strip_quotes(match.group("value"))
        if _LABEL_ONLY.match(line):
            continue
        normalized_line = _norm(line)
        if next_rendered and re.match(r"^[a-z0-9][).:\-]\s+", normalized_line):
            continue

        # During a backend-owned qualification turn the model contributes only
        # the acknowledgement. Any model-authored/paraphrased question or option
        # is discarded; the exact configured requirement is appended below.
        fragments = re.split(r"(?<=[.!?])\s+", line)
        for fragment in fragments:
            fragment = fragment.strip()
            if not fragment:
                continue
            normalized = _norm(fragment)
            if next_question and normalized == next_question:
                continue
            if next_rendered and (
                "?" in fragment
                or _QUESTION_FRAGMENT_RE.match(fragment)
                or re.match(r"^[a-z0-9][).:\-]\s+", normalized)
            ):
                continue
            if (
                option_values
                and re.match(r"^[a-z0-9][).:\-]\s+", normalized)
                and any(normalized.endswith(value) for value in option_values)
            ):
                continue
            kept.append(fragment)
    acknowledgement = " ".join(kept).strip()
    return (
        ""
        if _norm(acknowledgement).strip(" .!?") in _GENERIC_ACKS
        else acknowledgement
    )


def _stage_success(state: dict[str, Any]) -> bool:
    plan = state.get("response_plan") if isinstance(state, dict) else {}
    execution = plan.get("execution_results") if isinstance(plan, dict) else {}
    stage_info = execution.get("stage_transition") if isinstance(execution, dict) else {}
    result = stage_info.get("result") if isinstance(stage_info, dict) else {}
    target = (
        str(stage_info.get("target_stage_id") or "")
        if isinstance(stage_info, dict)
        else ""
    )
    stage = state.get("stage") if isinstance(state, dict) else {}
    return bool(
        stage_info
        and stage_info.get("configured")
        and target
        and isinstance(stage, dict)
        and str(stage.get("id") or "") == target
        and isinstance(result, dict)
        and result.get("verified") is True
        and _norm(result.get("status")) == "executed"
    )


def _leading_greeting(message: str) -> str:
    blocks = str(message or "").strip().split("\n\n")
    if not blocks:
        return ""
    first = blocks[0].strip()
    if not _GREETING_RE.match(first):
        return ""

    # Preserve legitimate welcome sentences, but stop before the model begins a
    # paraphrased qualification question/instruction. The backend appends the
    # exact configured requirement below.
    fragments = [
        fragment.strip()
        for fragment in re.split(r"(?<=[.!?])\s+", first)
        if fragment.strip()
    ]
    kept: list[str] = []
    for fragment in fragments:
        normalized = _norm(fragment)
        if (
            "?" in fragment
            or _QUESTION_FRAGMENT_RE.match(fragment)
            or "choose one" in normalized
            or "select one" in normalized
            or "please choose" in normalized
            or "please select" in normalized
        ):
            break
        kept.append(fragment)
    return " ".join(kept).strip()


def _finalize(decision, state):
    from apps.ai_engagement.services import canonical_architecture as canonical
    from apps.ai_engagement.services.engagement import EngagementError

    plan = state.get("response_plan") if isinstance(state, dict) else None
    if not isinstance(plan, dict):
        return decision

    kind = str(plan.get("response_type") or "")
    if kind not in {
        "qualification_start",
        "qualification_progress",
        "qualification_clarification",
        "qualification_complete",
    }:
        return decision

    rendered = str(
        ((plan.get("next_requirement") or {}).get("rendered") or "")
    ).strip()

    if kind == "qualification_start":
        if not rendered:
            raise EngagementError(
                "Qualification start plan is missing the first configured requirement."
            )
        greeting = _leading_greeting(getattr(decision, "message", ""))
        message = f"{greeting}\n\n{rendered}".strip() if greeting else rendered

    elif kind == "qualification_clarification":
        if not rendered:
            raise EngagementError(
                "Qualification clarification is missing the active configured requirement."
            )
        intro = _ack_from_message(getattr(decision, "message", ""), plan)
        message = f"{intro}\n\n{rendered}".strip() if intro else rendered

    else:
        acknowledgement = _ack_from_message(
            getattr(decision, "message", ""),
            plan,
        )
        raw_message = str(getattr(decision, "message", "") or "")
        has_internal_label = any(
            _LABEL_ONLY.match(line.strip())
            for line in raw_message.replace("\\n", "\n").splitlines()
            if line.strip()
        )
        if (
            plan.get("acknowledgement_required")
            and not acknowledgement
            and not has_internal_label
        ):
            acknowledgement = _fallback_acknowledgement(plan)
        if plan.get("acknowledgement_required") and not acknowledgement:
            raise EngagementError(
                "Qualification response requires a personalized acknowledgement."
            )

        if kind == "qualification_progress":
            if not rendered:
                raise EngagementError(
                    "Qualification progress plan is missing the next configured requirement."
                )
            message = f"{acknowledgement}\n\n{rendered}".strip()
        else:
            final_value = str(
                (
                    (plan.get("final_configured_acknowledgement") or {}).get("value")
                    or ""
                )
            ).strip()
            if not final_value:
                raise EngagementError(
                    "Qualification completion requires a configured Acknowledgment message value."
                )
            message = f"{acknowledgement}\n\n{final_value}".strip()

    if canonical._QUALIFIED_CLAIM_RE.search(message) and not _stage_success(state):
        message = canonical.ResponseActionValidator._replace_sentence(
            message,
            canonical._QUALIFIED_CLAIM_RE,
            "",
        )

    cleaned = []
    for raw in message.splitlines():
        line = raw.strip()
        if not line:
            cleaned.append("")
            continue
        match = _ACK_LABEL.match(line)
        if match:
            value = _strip_quotes(match.group("value"))
            if value:
                cleaned.append(value)
            continue
        if not _LABEL_ONLY.match(line):
            cleaned.append(line)
    final_message = "\n".join(cleaned).strip()
    next_id = str(
        ((plan.get("next_requirement") or {}).get("id") or "")
    ).strip() or None

    # The response-plan renderer is the final authority that actually places a
    # qualification question in the customer message. Keep decision metadata in
    # lock-step with that rendered output so the outbound post-save hook records
    # the exact question as ASKED. Without this, a language-only second pass can
    # append Q2 while leaving next_requirement_id=None, causing the customer's
    # correct Q2 answer to be rejected/repeated on the following turn.
    if kind == "qualification_complete":
        return replace(
            decision,
            message=final_message,
            next_requirement_id=None,
        )
    if kind == "qualification_clarification":
        return replace(
            decision,
            message=final_message,
            next_requirement_id=next_id,
            reason="QUALIFICATION_CLARIFY",
            reason_code="QUALIFICATION_CLARIFY",
        )
    return replace(
        decision,
        message=final_message,
        next_requirement_id=next_id,
        reason="QUALIFICATION_NEXT",
        reason_code="QUALIFICATION_NEXT",
    )


def _latest_inbound(lead, account_id=None):
    query = lead.whatsapp_messages.filter(direction="inbound")
    if account_id is not None:
        query = query.filter(account_id=account_id)
    return query.order_by("-created_at", "-id").first()


def install_qualification_execution_contract() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.services.engagement import EngagementService

    current_input = EngagementService._build_input
    current_instructions = EngagementService._build_instructions

    @wraps(current_input)
    def build_input(self, *, context, **kwargs):
        raw = current_input(self, context=context, **kwargs)
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return raw
        reconciled = payload.get("reconciled_state")
        if isinstance(reconciled, dict) and isinstance(
            reconciled.get("response_plan"),
            dict,
        ):
            payload["response_plan"] = deepcopy(reconciled["response_plan"])
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @wraps(current_instructions)
    def build_instructions(self, *, context, profile=None):
        base = current_instructions(self, context=context, profile=profile)
        return base if _PLAN_INSTRUCTIONS in base else f"{base}\n\n{_PLAN_INSTRUCTIONS}"

    EngagementService._build_input = build_input
    EngagementService._build_instructions = build_instructions

    from apps.ai_engagement.services import transactional_turn_runtime as runtime
    from apps.ai_engagement.services.canonical_architecture import (
        ResponseActionValidator,
        StateReconciler,
    )

    current_resolve = runtime._resolve_state_before_response
    reconciler = StateReconciler()

    @wraps(current_resolve)
    def resolve(*, organization, lead, source_message_id, decision, account_id=None):
        result = current_resolve(
            organization=organization,
            lead=lead,
            source_message_id=source_message_id,
            decision=decision,
            account_id=account_id,
        )
        snapshot = result.get("reconciled_state") if isinstance(result, dict) else None
        if not result or not result.get("applied") or not isinstance(snapshot, dict):
            return result

        lead.refresh_from_db(fields=["attributes", "pipeline", "stage"])
        plan = _plan_from_reconciled(
            lead=lead,
            source_message_id=source_message_id,
            snapshot=snapshot,
        )
        if not plan:
            return result
        snapshot = {**snapshot, "response_plan": plan}
        reconciler.persist_for_source(
            lead=lead,
            source_message_id=source_message_id,
            snapshot=snapshot,
        )
        return {**result, "reconciled_state": snapshot}

    runtime._resolve_state_before_response = resolve

    current_validate = ResponseActionValidator.validate

    @wraps(current_validate)
    def validate(self, *, decision, reconciled_state):
        validated = current_validate(
            self,
            decision=decision,
            reconciled_state=reconciled_state,
        )
        return _finalize(
            validated,
            reconciled_state if isinstance(reconciled_state, dict) else {},
        )

    ResponseActionValidator.validate = validate

    from apps.ai_engagement import tasks as task_module
    from apps.ai_engagement.services.ai_permissions import AIPermissionService
    from apps.crm.models import Lead
    from services.channels.whatsapp_service import resolve_account_for_lead

    current_task = task_module._execute_ai_engagement_response_impl

    @wraps(current_task)
    def execute(*, task, lead_id: str):
        lead = (
            Lead.objects.select_related("organization", "pipeline", "stage")
            .filter(pk=lead_id)
            .first()
        )
        if lead is not None:
            source = _latest_inbound(lead)
            try:
                permission = AIPermissionService().evaluate(
                    organization=lead.organization,
                    lead=lead,
                    latest_inbound=source,
                )
            except Exception:
                permission = None
            account = (
                resolve_account_for_lead(
                    organization=lead.organization,
                    lead=lead,
                )
                if permission and permission.allowed
                else None
            )
            if source is not None and account is not None:
                try:
                    resolve_before_generation(
                        organization=lead.organization,
                        lead=lead,
                        source_message_id=source.pk,
                    )
                except Exception:
                    logger.exception(
                        "Qualification pre-generation execution failed for lead %s",
                        lead_id,
                    )
        return current_task(task=task, lead_id=lead_id)

    task_module._execute_ai_engagement_response_impl = execute

    from apps.hosted_automation import execution as hosted_execution

    current_hosted = hosted_execution.execute_hosted_ai_engagement

    @wraps(current_hosted)
    def execute_hosted(*, task, job):
        lead = (
            Lead.objects.select_related("organization", "pipeline", "stage")
            .filter(
                pk=job.lead_id,
                organization_id=job.organization_id,
            )
            .first()
        )
        if lead is not None:
            source = _latest_inbound(lead, account_id=job.account_id)
            try:
                permission = AIPermissionService().evaluate(
                    organization=lead.organization,
                    lead=lead,
                    latest_inbound=source,
                )
            except Exception:
                permission = None
            account = hosted_execution._connected_hosted_account(
                account_id=job.account_id,
                organization_id=job.organization_id,
            )
            if (
                permission
                and permission.allowed
                and account is not None
                and source is not None
                and str(source.pk) == str(job.source_message_id)
            ):
                try:
                    resolve_before_generation(
                        organization=lead.organization,
                        lead=lead,
                        source_message_id=source.pk,
                        account_id=job.account_id,
                    )
                except Exception:
                    logger.exception(
                        "Hosted qualification pre-generation execution failed for lead %s",
                        job.lead_id,
                    )
        return current_hosted(task=task, job=job)

    hosted_execution.execute_hosted_ai_engagement = execute_hosted
    _INSTALLED = True
