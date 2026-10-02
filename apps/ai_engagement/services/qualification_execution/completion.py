"""Qualification completion and reminder resolution."""

from __future__ import annotations

import re
from typing import Any

from .common import _norm


def _completion_target(*, lead, state: dict[str, Any], config: dict[str, Any]):
    """Resolve the qualification-completion stage deterministically.

    Criteria must pass before an authored completion target or a uniquely
    configured same-pipeline Qualified stage is eligible. Never guess a stage.
    """
    from apps.ai_engagement.services.playbook import criteria_for_lead
    if not criteria_for_lead(lead=lead, state=state).get("qualified"):
        return None

    if config.get("completion_routes"):
        resolved = {}
        for route in config["completion_routes"]:
            if not route.get("source_supported", True):
                return None
            sources = route.get("sources")
            if sources is not None and str(getattr(lead, "lead_source", "")) not in sources:
                continue
            candidates = route.get("targets") or []
            if route.get("current_pipeline_only"):
                candidates = [item for item in candidates if str(item.get("pipeline_id")) == str(lead.pipeline_id)]
            if len(candidates) != 1:
                return None
            resolved[str(candidates[0]["id"])] = candidates[0]
        return next(iter(resolved.values())) if len(resolved) == 1 else None

    target = config.get("completion_stage")
    explicit_targets = (
        [target] if isinstance(target, dict) and target.get("id") is not None else []
    )
    for candidates in config.get("current_pipeline_completion_targets") or []:
        local = [
            candidate for candidate in candidates
            if str(candidate.get("pipeline_id")) == str(lead.pipeline_id)
        ]
        if len(local) != 1:
            return None
        explicit_targets.extend(local)
    unique = {str(item["id"]): item for item in explicit_targets}
    stage_errors = any(
        item.get("code") in {
            "unresolved_completion_stage_rule", "ambiguous_completion_stage_rule",
        }
        for item in config.get("errors") or []
    )
    if stage_errors or len(unique) > 1:
        return None
    if len(unique) == 1:
        return next(iter(unique.values()))
    if config.get("completion_rules"):
        # An unresolved authored target must not silently become Qualified.
        return None

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

    return None


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


def _configured_completion_reminders(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Build one deterministic follow-up reminder when qualification completes.

    Only an explicit organization-authored completion reminder creates an action.
    Its due time must be grounded in that rule.
    """
    from apps.ai_engagement.services.reminder_time_runtime import parse_grounded_due_at

    actions: list[dict[str, Any]] = []
    for rule in config.get("reminder_rules") or []:
        text = str(rule or "").strip()
        if not text or not _COMPLETION_REMINDER_SCOPE_RE.search(text):
            continue
        # This deterministic path proves completion only. A prohibition or an
        # extra condition must not be turned into an unconditional reminder.
        if re.search(r"\b(?:do not|don't|never|unless|except|if|provided that)\b", text, re.I):
            continue
        if not _REMINDER_CREATE_RE.search(text):
            continue

        due_at = parse_grounded_due_at(text)
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

    return actions
