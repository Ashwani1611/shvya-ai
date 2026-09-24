"""Small AI maintenance/recovery Celery boundaries."""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="ai.flush_background_enrichment")
def flush_background_enrichment(lead_id):
    from apps.ai_engagement.services.background_enrichment import (
        queue_background_enrichment,
    )

    return queue_background_enrichment(lead_id=lead_id, force=True)


@shared_task(name="ai.reconcile_credit_settlements")
def reconcile_credit_settlements():
    """Retry provider-completed AI credit reservations that failed to settle."""
    from apps.ai_engagement.services.credits import AICreditService

    result = AICreditService.reconcile_pending_settlements(limit=100)
    if result["failed"]:
        logger.warning(
            "AI credit settlement reconciliation left %s reservation(s) pending",
            result["failed"],
        )
    return result


@shared_task(name="ai.recover_api_engagement")
def recover_api_engagement():
    from apps.ai_engagement.services.execution_tracker import (
        recover_api_engagement as recover,
    )

    return recover()
