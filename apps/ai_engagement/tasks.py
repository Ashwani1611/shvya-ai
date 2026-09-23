from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from apps.ai_engagement.prompts import BUMP_UP_MESSAGE_INSTRUCTIONS
from apps.ai_engagement.services.engagement_execution import (
    _execute_ai_engagement_response_impl,
    _has_existing_ai_response,
    _latest_whatsapp_message,
    _persist_engagement_answers,
    _whatsapp_send_eligible,
)
from apps.ai_engagement.task_handlers.knowledge import (
    ingest_and_index_document,
    ingest_and_index_url_source,
    reindex_document_embeddings,
)
from apps.ai_engagement.task_handlers.qualification import generate_lead_qualification
from apps.ai_engagement.task_handlers.summaries import (
    generate_internal_conversation_summary,
)


logger = logging.getLogger(__name__)

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


@shared_task(name="ai.flush_background_enrichment")
def flush_background_enrichment(lead_id):
    from apps.ai_engagement.services.background_enrichment import queue_background_enrichment

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


@shared_task(name="ai.dispatch_bump_ups")
def dispatch_bump_ups():
    """Queue at most one AI-written bump per lead after each silent hour."""
    from apps.ai_engagement.services.ai_permissions import AIPermissionService
    from apps.ai_engagement.services.engagement import EngagementService
    from apps.ai_engagement.services.org_info import OrgInfoService
    from apps.channels.models import WhatsAppMessage
    from apps.channels.tasks import send_whatsapp_message_task
    from apps.crm.models import Lead
    from services.channels.hosted_whatsapp_service import get_session_settings
    from services.channels.whatsapp_service import queue_outbound_message, resolve_account_for_lead

    now = timezone.now()
    queued = 0
    leads = Lead.objects.select_related("organization", "pipeline", "stage").filter(
        organization__is_active=True,
        pipeline__is_active=True,
        ai_enabled=True,
    ).iterator(chunk_size=200)

    for lead in leads:
        if not AIPermissionService().evaluate(organization=lead.organization, lead=lead).allowed:
            continue
        org_info = OrgInfoService().get_or_create(organization=lead.organization)
        if not org_info.bump_up_enabled or org_info.bump_up_count < 1:
            continue
        account = resolve_account_for_lead(organization=lead.organization, lead=lead)
        if account is None:
            continue
        account_settings = get_session_settings(account=account)
        if not account_settings.get("ai_auto_reply") or not account_settings.get("bump_up_messages"):
            continue

        recent = list(lead.whatsapp_messages.order_by("-created_at", "-id")[:100])
        if not recent or recent[0].direction != WhatsAppMessage.Direction.OUTBOUND:
            continue
        if recent[0].created_at > now - timedelta(hours=1):
            continue
        latest_inbound = next(
            (message for message in recent if message.direction == WhatsAppMessage.Direction.INBOUND),
            None,
        )
        if latest_inbound is None or latest_inbound.created_at <= now - timedelta(hours=24):
            continue
        bump_messages = [
            message for message in recent
            if isinstance(message.raw_payload, dict)
            and message.raw_payload.get("shvya_ai", {}).get("origin") == "bump_up"
            and message.created_at > latest_inbound.created_at
        ]
        limit = min(int(org_info.bump_up_count), int(account_settings.get("bump_up_count", 1)))
        if len(bump_messages) >= limit:
            continue

        service = EngagementService()
        try:
            context = service.context_builder.build(
                organization=lead.organization,
                lead=lead,
                knowledge_query=recent[0].body,
            )
            instructions = (
                service._build_instructions(context=context)
                + "\n\n"
                + BUMP_UP_MESSAGE_INSTRUCTIONS
            )
            provider = service.provider or __import__(
                "apps.ai_engagement.services.ai_provider", fromlist=["OpenAIProvider"]
            ).OpenAIProvider()
            result = provider.generate_text(
                instructions=instructions,
                input_text=service._build_input(context=context),
                metadata={"organization_id": str(lead.organization_id), "lead_id": str(lead.id), "task": "bump_up"},
            )
            decision = service._normalize_result(result=result)
            service._validate_engagement_policy(decision=decision, context=context)
        except Exception:
            logger.exception("Unable to generate bump-up for lead %s", lead.id)
            continue
        if not decision.should_engage or not decision.message.strip():
            continue

        with transaction.atomic():
            locked_lead = Lead.objects.select_for_update().select_related(
                "organization", "pipeline", "stage"
            ).get(pk=lead.pk)
            latest = locked_lead.whatsapp_messages.order_by("-created_at", "-id").first()
            if latest is None or latest.id != recent[0].id:
                continue
            from services.channels.hosted_whatsapp_service import account_ai_block_reason

            if account_ai_block_reason(
                account=account, lead=locked_lead, bump_up_number=len(bump_messages) + 1,
            ):
                continue
            outbound = queue_outbound_message(
                organization=locked_lead.organization,
                account=account,
                to_number=locked_lead.phone,
                body=decision.message.strip(),
                lead=locked_lead,
            )
            outbound.raw_payload = {"shvya_ai": {"origin": "bump_up", "number": len(bump_messages) + 1, "model": decision.model}}
            outbound.save(update_fields=["raw_payload", "updated_at"])
            transaction.on_commit(lambda message_id=str(outbound.id): send_whatsapp_message_task.delay(message_id))
            queued += 1
    return {"queued": queued}


# ============================================================
# INTERNAL CONVERSATION SUMMARY
# ============================================================


# ============================================================
# INTERNAL CONVERSATION SUMMARY
# ============================================================
# The task itself is implemented in task_handlers.summaries and re-exported
# above. Keeping this boundary preserves historical imports and patch paths.


def _execute_ai_engagement_response(*, task, lead_id):
    from celery.exceptions import Retry
    from apps.channels.models import WhatsAppMessage
    from apps.ai_engagement.services.execution_tracker import (
        claim_execution,
        record_execution,
    )
    source = WhatsAppMessage.objects.filter(
        lead_id=lead_id,
        direction="inbound",
    ).order_by("-created_at", "-id").first()
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
            record_execution(source.pk, status="retrying", reason="temporary_failure")
        raise
    except Exception:
        if source:
            record_execution(source.pk, status="failed", reason="execution_error")
        raise
    if source:
        record_execution(source.pk, status=str((result or {}).get("status") or "failed"),
            reason=str((result or {}).get("reason") or ""))
    return result


@shared_task(name="ai.recover_api_engagement")
def recover_api_engagement():
    from apps.ai_engagement.services.execution_tracker import recover_api_engagement as recover
    return recover()


# ============================================================
# CANONICAL AI ENGAGEMENT TASK
# ============================================================


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    name="ai.generate_ai_engagement_response",
)
def generate_ai_engagement_response(
    self,
    lead_id: str,
):
    """
    Canonical production AI Engagement worker.

    Celery payload contains Lead ID only.
    """
    return _execute_ai_engagement_response(
        task=self,
        lead_id=lead_id,
    )


# ============================================================
# KNOWLEDGE INGESTION — UPLOADED DOCUMENT
# ============================================================

