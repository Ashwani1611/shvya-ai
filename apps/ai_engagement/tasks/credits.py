"""AI credit reconciliation task boundary."""

import logging

from celery import shared_task


logger = logging.getLogger(__name__)


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
