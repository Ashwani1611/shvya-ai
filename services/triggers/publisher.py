import uuid

from apps.triggers.models import SmartTrigger


def queue_trigger_event(
    *,
    organization_id,
    lead_id,
    event_type,
    payload=None,
    event_id=None,
):
    """
    Queue an event only when at least one active rule can consume it.

    The existence check avoids adding Celery/Redis work to ordinary CRM writes
    for organizations that have not configured Smart Triggers.
    """
    if event_type not in SmartTrigger.EventType.values:
        return None

    has_listener = SmartTrigger.objects.filter(
        organization_id=organization_id,
        event_type=event_type,
        is_active=True,
    ).exists()
    if not has_listener:
        return None

    from apps.triggers.tasks import process_smart_trigger_event

    resolved_event_id = str(event_id or uuid.uuid4())
    process_smart_trigger_event.delay(
        resolved_event_id,
        str(organization_id),
        str(lead_id),
        event_type,
        payload or {},
    )
    return resolved_event_id
