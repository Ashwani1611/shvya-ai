"""Background enrichment task boundary."""

from celery import shared_task

@shared_task(name="ai.flush_background_enrichment")
def flush_background_enrichment(lead_id):
    from apps.ai_engagement.services.background_enrichment import queue_background_enrichment

    return queue_background_enrichment(lead_id=lead_id, force=True)
