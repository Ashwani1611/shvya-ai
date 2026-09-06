from celery import shared_task

from services.triggers.evaluator import process_event


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
)
def process_smart_trigger_event(
    self,
    event_id,
    organization_id,
    lead_id,
    event_type,
    payload=None,
):
    """Celery entry point for queue-first Smart Trigger evaluation."""
    return process_event(
        event_id=event_id,
        organization_id=organization_id,
        lead_id=lead_id,
        event_type=event_type,
        payload=payload or {},
    )
