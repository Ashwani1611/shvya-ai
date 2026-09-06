from __future__ import annotations

import uuid
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.crm.models import Lead
from apps.triggers.models import SmartTrigger, TriggerExecution
from services.triggers.actions import TriggerActionError, execute_actions, validate_actions


class TriggerConfigurationError(Exception):
    pass


OPERATORS = {
    "equals",
    "not_equals",
    "contains",
    "not_contains",
    "starts_with",
    "ends_with",
    "is_empty",
    "is_not_empty",
}

BASE_FIELDS = {
    "lead.name",
    "lead.phone",
    "lead.email",
    "lead.lead_source",
    "lead.pipeline_name",
    "lead.stage_name",
    "lead.notes",
    "lead.ai_enabled",
    "event.message_body",
    "event.changed_fields",
}


def validate_conditions(conditions):
    if conditions in (None, ""):
        return []
    if not isinstance(conditions, list):
        raise TriggerConfigurationError("Conditions must be a list.")
    if len(conditions) > 20:
        raise TriggerConfigurationError(
            "A Smart Trigger can have at most 20 conditions."
        )

    for index, condition in enumerate(conditions, start=1):
        if not isinstance(condition, dict):
            raise TriggerConfigurationError(f"Condition {index} is invalid.")
        field = str(condition.get("field") or "").strip()
        operator = str(condition.get("operator") or "").strip()
        if field not in BASE_FIELDS and not field.startswith("attr."):
            raise TriggerConfigurationError(
                f"Condition {index} uses an unsupported field."
            )
        if field.startswith("attr.") and not field[5:].strip():
            raise TriggerConfigurationError(
                f"Condition {index} needs a custom attribute key."
            )
        if operator not in OPERATORS:
            raise TriggerConfigurationError(
                f"Condition {index} uses an unsupported operator."
            )
        if operator not in {"is_empty", "is_not_empty"} and "value" not in condition:
            raise TriggerConfigurationError(
                f"Condition {index} needs a comparison value."
            )
    return conditions


def validate_trigger_configuration(
    *,
    event_type,
    condition_mode,
    conditions,
    actions,
    organization=None,
):
    if event_type not in SmartTrigger.EventType.values:
        raise TriggerConfigurationError("Choose a valid trigger event.")
    if condition_mode not in SmartTrigger.ConditionMode.values:
        raise TriggerConfigurationError("Choose ALL or ANY condition matching.")
    validate_conditions(conditions)
    try:
        validate_actions(actions, organization=organization)
    except TriggerActionError as exc:
        raise TriggerConfigurationError(str(exc)) from exc


def _normalize(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, set)):
        return ",".join(str(item) for item in value)
    return str(value)


def _resolve_field(*, field, lead, payload):
    if field == "lead.name":
        return lead.name
    if field == "lead.phone":
        return lead.phone
    if field == "lead.email":
        return lead.email
    if field == "lead.lead_source":
        return lead.lead_source
    if field == "lead.pipeline_name":
        return lead.pipeline.name if lead.pipeline_id else ""
    if field == "lead.stage_name":
        return lead.stage.name if lead.stage_id else ""
    if field == "lead.notes":
        return lead.notes
    if field == "lead.ai_enabled":
        return lead.ai_enabled
    if field == "event.message_body":
        return payload.get("message_body", "")
    if field == "event.changed_fields":
        return payload.get("changed_fields", [])
    if field.startswith("attr."):
        return (lead.attributes or {}).get(field[5:], "")
    return ""


def _matches_condition(*, condition, lead, payload):
    actual = _normalize(
        _resolve_field(
            field=condition["field"],
            lead=lead,
            payload=payload,
        )
    )
    expected = _normalize(condition.get("value", ""))
    operator = condition["operator"]

    if operator == "is_empty":
        return actual.strip() == ""
    if operator == "is_not_empty":
        return actual.strip() != ""

    actual_folded = actual.casefold()
    expected_folded = expected.casefold()
    if operator == "equals":
        return actual_folded == expected_folded
    if operator == "not_equals":
        return actual_folded != expected_folded
    if operator == "contains":
        return expected_folded in actual_folded
    if operator == "not_contains":
        return expected_folded not in actual_folded
    if operator == "starts_with":
        return actual_folded.startswith(expected_folded)
    if operator == "ends_with":
        return actual_folded.endswith(expected_folded)
    return False


def conditions_match(*, trigger, lead, payload):
    conditions = list(trigger.conditions or [])
    if not conditions:
        return True
    results = [
        _matches_condition(condition=item, lead=lead, payload=payload)
        for item in conditions
    ]
    if trigger.condition_mode == SmartTrigger.ConditionMode.ANY:
        return any(results)
    return all(results)


def _finish_skipped(execution, reason):
    execution.status = TriggerExecution.Status.SKIPPED
    execution.matched = False
    execution.skip_reason = reason[:255]
    execution.finished_at = timezone.now()
    execution.save(
        update_fields=[
            "status",
            "matched",
            "skip_reason",
            "finished_at",
        ]
    )


def _frequency_skip_reason(trigger, lead, now):
    successes = TriggerExecution.objects.filter(
        trigger=trigger,
        lead=lead,
        status=TriggerExecution.Status.SUCCESS,
    )
    if trigger.once_per_lead and successes.exists():
        return "Configured to run only once per lead."
    if trigger.cooldown_minutes:
        cutoff = now - timedelta(minutes=trigger.cooldown_minutes)
        if successes.filter(finished_at__gte=cutoff).exists():
            return f"Lead is inside the {trigger.cooldown_minutes}-minute cooldown."
    return ""


def _begin_execution(*, trigger, lead, event_id, event_type, payload, started_at):
    try:
        with transaction.atomic():
            return TriggerExecution.objects.create(
                organization=trigger.organization,
                trigger=trigger,
                lead=lead,
                event_id=event_id,
                event_type=event_type,
                event_payload=payload,
                status=TriggerExecution.Status.PROCESSING,
                started_at=started_at,
            )
    except IntegrityError:
        return None


def _evaluate_one(*, trigger, lead, event_id, event_type, payload):
    started_at = timezone.now()
    execution = _begin_execution(
        trigger=trigger,
        lead=lead,
        event_id=event_id,
        event_type=event_type,
        payload=payload,
        started_at=started_at,
    )
    if execution is None:
        return None

    reason = _frequency_skip_reason(trigger, lead, started_at)
    if reason:
        _finish_skipped(execution, reason)
        return execution

    try:
        validate_trigger_configuration(
            event_type=trigger.event_type,
            condition_mode=trigger.condition_mode,
            conditions=trigger.conditions,
            actions=trigger.actions,
            organization=trigger.organization,
        )
        if not conditions_match(trigger=trigger, lead=lead, payload=payload):
            _finish_skipped(execution, "Conditions did not match.")
            return execution

        action_results = execute_actions(
            trigger=trigger,
            lead=lead,
            actions=trigger.actions,
        )
        finished_at = timezone.now()
        execution.status = TriggerExecution.Status.SUCCESS
        execution.matched = True
        execution.action_results = action_results
        execution.finished_at = finished_at
        execution.save(
            update_fields=[
                "status",
                "matched",
                "action_results",
                "finished_at",
            ]
        )
        SmartTrigger.objects.filter(id=trigger.id).update(
            successful_runs=F("successful_runs") + 1,
            last_fired_at=finished_at,
        )
    except Exception as exc:
        finished_at = timezone.now()
        execution.status = TriggerExecution.Status.FAILED
        execution.matched = True
        execution.error = str(exc)
        execution.finished_at = finished_at
        execution.save(
            update_fields=[
                "status",
                "matched",
                "error",
                "finished_at",
            ]
        )
        SmartTrigger.objects.filter(id=trigger.id).update(
            failed_runs=F("failed_runs") + 1,
            last_fired_at=finished_at,
        )
    return execution


def process_event(*, event_id, organization_id, lead_id, event_type, payload=None):
    """Evaluate all active organization rules for one queued CRM event."""
    if event_type not in SmartTrigger.EventType.values:
        raise TriggerConfigurationError("Unsupported Smart Trigger event type.")

    try:
        resolved_event_id = uuid.UUID(str(event_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise TriggerConfigurationError("Invalid Smart Trigger event id.") from exc

    lead = (
        Lead.objects.select_related("organization", "pipeline", "stage")
        .filter(id=lead_id, organization_id=organization_id)
        .first()
    )
    if not lead:
        return {"evaluated": 0, "executions": []}

    payload = payload if isinstance(payload, dict) else {}
    triggers = list(
        SmartTrigger.objects.select_related("organization", "created_by")
        .filter(
            organization_id=organization_id,
            event_type=event_type,
            is_active=True,
        )
        .order_by("created_at")
    )

    execution_ids = []
    for trigger in triggers:
        execution = _evaluate_one(
            trigger=trigger,
            lead=lead,
            event_id=resolved_event_id,
            event_type=event_type,
            payload=payload,
        )
        if execution is not None:
            execution_ids.append(str(execution.id))

    return {"evaluated": len(triggers), "executions": execution_ids}
