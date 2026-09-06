from django.db import transaction

from apps.triggers.models import SmartTrigger
from services.triggers.evaluator import (
    TriggerConfigurationError,
    validate_trigger_configuration,
)


MAX_COOLDOWN_MINUTES = 525600


def _clean_common(
    *,
    organization,
    name,
    description,
    event_type,
    condition_mode,
    conditions,
    actions,
    is_active,
    once_per_lead,
    cooldown_minutes,
    exclude_id=None,
):
    name = (name or "").strip()
    description = (description or "").strip()
    if not name:
        raise TriggerConfigurationError("Trigger name is required.")
    if len(name) > 255:
        raise TriggerConfigurationError("Trigger name must be 255 characters or fewer.")
    if len(description) > 300:
        raise TriggerConfigurationError("Description must be 300 characters or fewer.")

    try:
        cooldown_minutes = int(cooldown_minutes or 0)
    except (TypeError, ValueError) as exc:
        raise TriggerConfigurationError("Cooldown must be a whole number of minutes.") from exc
    if cooldown_minutes < 0 or cooldown_minutes > MAX_COOLDOWN_MINUTES:
        raise TriggerConfigurationError(
            "Cooldown must be between 0 and 525600 minutes."
        )

    duplicate = SmartTrigger.objects.filter(
        organization=organization,
        name__iexact=name,
    )
    if exclude_id:
        duplicate = duplicate.exclude(id=exclude_id)
    if duplicate.exists():
        raise TriggerConfigurationError("A Smart Trigger with this name already exists.")

    validate_trigger_configuration(
        event_type=event_type,
        condition_mode=condition_mode,
        conditions=conditions,
        actions=actions,
        organization=organization,
    )
    return {
        "name": name,
        "description": description,
        "event_type": event_type,
        "condition_mode": condition_mode,
        "conditions": conditions,
        "actions": actions,
        "is_active": bool(is_active),
        "once_per_lead": bool(once_per_lead),
        "cooldown_minutes": cooldown_minutes,
    }


@transaction.atomic
def create_trigger(*, organization, created_by, **values):
    cleaned = _clean_common(organization=organization, **values)
    return SmartTrigger.objects.create(
        organization=organization,
        created_by=created_by,
        **cleaned,
    )


@transaction.atomic
def update_trigger(*, trigger, **values):
    cleaned = _clean_common(
        organization=trigger.organization,
        exclude_id=trigger.id,
        **values,
    )
    for field, value in cleaned.items():
        setattr(trigger, field, value)
    trigger.save(
        update_fields=[
            *cleaned.keys(),
            "updated_at",
        ]
    )
    return trigger


@transaction.atomic
def duplicate_trigger(*, trigger, created_by):
    base = f"{trigger.name} copy"
    name = base
    suffix = 2
    while SmartTrigger.objects.filter(
        organization=trigger.organization,
        name__iexact=name,
    ).exists():
        name = f"{base} {suffix}"
        suffix += 1

    return SmartTrigger.objects.create(
        organization=trigger.organization,
        created_by=created_by,
        name=name,
        description=trigger.description,
        event_type=trigger.event_type,
        condition_mode=trigger.condition_mode,
        conditions=list(trigger.conditions or []),
        actions=list(trigger.actions or []),
        is_active=False,
        once_per_lead=trigger.once_per_lead,
        cooldown_minutes=trigger.cooldown_minutes,
    )
