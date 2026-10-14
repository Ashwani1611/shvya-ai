"""Platform housekeeping on the existing general worker."""

from celery import shared_task

from apps.core.api_activity.retention import prune_activities
from apps.core.observability import emit_event


@shared_task(name="core.prune_api_activities", ignore_result=True, soft_time_limit=60, time_limit=90)
def prune_api_activities():
    # A bounded invocation leaves any larger backlog for the next scheduled run.
    result = prune_activities(batch_size=500, max_batches=20, max_seconds=30)
    if result.get("has_more"):
        emit_event("api_activity.retention_backlog")
    return result


@shared_task(name="core.discover_api_incidents", ignore_result=True, soft_time_limit=45, time_limit=60)
def discover_api_incidents():
    """Open bounded operator reviews; do not retry any customer operation."""
    from django.conf import settings
    if not getattr(settings, "API_ACTIVITY_ENABLED", True):
        return {"skipped": "capture disabled"}
    from apps.core.api_activity.health import discover_attention
    # Do not age unreviewed failures or long-running overdue work out of the scan.
    return discover_attention(limit=200)
