"""Stable Celery task facade for SHVYA AI engagement.

Public imports and Celery task names remain compatible. Focused task boundaries
live under apps.ai_engagement.task_handlers; this module keeps the historical
patch surface used by qualification runtime installation.
"""

from celery import shared_task

from apps.ai_engagement.services.engagement_execution import (
    _execute_ai_engagement_response_impl,
    _has_existing_ai_response,
    _latest_whatsapp_message,
    _persist_engagement_answers,
    _whatsapp_send_eligible,
)
from apps.ai_engagement.task_handlers.bumpups import dispatch_bump_ups
from apps.ai_engagement.task_handlers.knowledge import (
    ingest_and_index_document,
    ingest_and_index_url_source,
    reindex_document_embeddings,
)
from apps.ai_engagement.task_handlers.maintenance import (
    flush_background_enrichment,
    reconcile_credit_settlements,
    recover_api_engagement,
)
from apps.ai_engagement.task_handlers.qualification import generate_lead_qualification
from apps.ai_engagement.task_handlers.summaries import (
    generate_internal_conversation_summary,
)

__all__ = [
    "_execute_ai_engagement_response",
    "_execute_ai_engagement_response_impl",
    "_has_existing_ai_response",
    "_latest_whatsapp_message",
    "_persist_engagement_answers",
    "_whatsapp_send_eligible",
    "dispatch_bump_ups",
    "flush_background_enrichment",
    "generate_ai_engagement_response",
    "generate_internal_conversation_summary",
    "generate_lead_qualification",
    "ingest_and_index_document",
    "ingest_and_index_url_source",
    "reconcile_credit_settlements",
    "recover_api_engagement",
    "reindex_document_embeddings",
]


def _execute_ai_engagement_response(*, task, lead_id):
    """Claim one inbound turn, execute it once, and record durable outcome."""
    from celery.exceptions import Retry

    from apps.ai_engagement.services.execution_tracker import (
        claim_execution,
        record_execution,
    )
    from apps.channels.models import WhatsAppMessage

    source = (
        WhatsAppMessage.objects.filter(
            lead_id=lead_id,
            direction="inbound",
        )
        .order_by("-created_at", "-id")
        .first()
    )
    if source and not claim_execution(source.pk):
        return {
            "status": "skipped",
            "reason": "duplicate_turn_in_progress_or_processed",
            "lead_id": str(lead_id),
            "source_message_id": str(source.pk),
        }

    try:
        result = _execute_ai_engagement_response_impl(task=task, lead_id=lead_id)
    except Retry:
        if source:
            record_execution(
                source.pk,
                status="retrying",
                reason="temporary_failure",
            )
        raise
    except Exception:
        if source:
            record_execution(
                source.pk,
                status="failed",
                reason="execution_error",
            )
        raise

    if source:
        record_execution(
            source.pk,
            status=str((result or {}).get("status") or "failed"),
            reason=str((result or {}).get("reason") or ""),
        )
    return result


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    name="ai.generate_ai_engagement_response",
)
def generate_ai_engagement_response(self, lead_id: str):
    """Canonical production AI engagement worker; payload contains Lead ID only."""
    from django.conf import settings

    from apps.core.fairness import admit_ai_start
    from apps.crm.models import Lead

    organization_id = (
        Lead.objects.filter(pk=lead_id)
        .values_list("organization_id", flat=True)
        .first()
    )
    if organization_id:
        allowed, retry_after, scope = admit_ai_start(
            organization_id=organization_id,
            organization_limit=settings.AI_ORGANIZATION_STARTS_PER_MINUTE,
            global_limit=settings.AI_GLOBAL_STARTS_PER_MINUTE,
        )
        if not allowed:
            # Re-publish rather than consume the task retry budget. The
            # canonical execution/idempotency claim has not started yet.
            self.apply_async(
                args=[str(lead_id)],
                countdown=int(retry_after)
                + (sum(ord(character) for character in str(lead_id)) % 5),
            )
            return {
                "status": "deferred",
                "reason": f"{scope}_fairness_limit",
                "lead_id": str(lead_id),
            }

    return _execute_ai_engagement_response(task=self, lead_id=lead_id)
