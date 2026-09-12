"""Celery task for automatic welcomes triggered by CRM lead creation."""

import logging

from celery import shared_task

from apps.ai_engagement.services.ai_provider import AIProviderError

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=20)
def send_lead_welcome_task(self, lead_id):
    """Generate/queue the correct welcome without blocking lead creation."""
    from services.channels.welcome_message_service import send_new_lead_welcome

    try:
        return send_new_lead_welcome(lead_id=lead_id)
    except AIProviderError as exc:
        if getattr(exc, "retryable", False):
            raise self.retry(exc=exc)
        logger.warning(
            "New-lead welcome AI generation failed permanently for lead %s: %s",
            lead_id,
            exc,
        )
        return {
            "status": "failed",
            "reason": "ai_generation_failed",
            "error": str(exc),
        }
    except Exception:
        logger.exception("New-lead welcome failed for lead %s", lead_id)
        return {"status": "failed", "reason": "unexpected_error"}
