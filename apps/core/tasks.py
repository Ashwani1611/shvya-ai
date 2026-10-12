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
