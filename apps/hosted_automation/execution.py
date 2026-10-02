"""Account-scoped execution for durable Hosted WhatsApp AI jobs.

Hosted jobs own an exact WhatsApp account and source inbound message.  This
module keeps that context explicit for every permission, conversation, duplicate,
and outbound operation instead of temporarily replacing process-global helpers.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import Q

from apps.ai_engagement.services.ai_provider import (
    AIProviderTransientError,
    provider_retry_countdown,
)
from apps.ai_engagement.services.ai_permissions import AIPermissionError, AIPermissionService
from apps.ai_engagement.services.context import AIContextBuilder
from apps.ai_engagement.services.crm_executor import CRMActionExecutionError, CRMActionExecutor
from apps.ai_engagement.services.engagement import EngagementError, EngagementService
from apps.ai_engagement.services.engagement_failsoft import (
    build_deterministic_fallback_decision,
)
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead
from services.channels.whatsapp_service import queue_outbound_message


logger = logging.getLogger(__name__)


class HostedAIContextBuilder(AIContextBuilder):
    """Build AI conversation context from one exact Hosted account only."""

    def __init__(self, *, account_id):
        self.account_id = account_id

    def _get_messages(self, *, organization, lead, limit):
        messages = list(
            WhatsAppMessage.objects.filter(
                organization=organization,
                account_id=self.account_id,
                lead=lead,
            ).exclude(
                Q(direction=WhatsAppMessage.Direction.OUTBOUND)
                & ~Q(status__in=[WhatsAppMessage.Status.SENT, WhatsAppMessage.Status.DELIVERED, WhatsAppMessage.Status.READ])
            ).order_by("-created_at", "-id")[:limit]
        )
        messages.reverse()
        return messages

    def latest_inbound_for_fallback(self, *, organization, lead):
        return (
            WhatsAppMessage.objects.filter(
                organization=organization,
                account_id=self.account_id,
                lead=lead,
                direction=WhatsAppMessage.Direction.INBOUND,
            )
            .select_related("account")
            .order_by("-created_at", "-id")
            .first()
        )


def _latest_for_account(*, lead, account):
    inbound = (
        lead.whatsapp_messages.filter(
            organization_id=lead.organization_id,
            account=account,
            direction=WhatsAppMessage.Direction.INBOUND,
        )
        .order_by("-created_at", "-id")
        .first()
    )
    if inbound is None:
        return None
    human_reply = lead.whatsapp_messages.filter(
        organization_id=lead.organization_id, account=account,
        direction=WhatsAppMessage.Direction.OUTBOUND,
        status__in=[WhatsAppMessage.Status.SENT, WhatsAppMessage.Status.DELIVERED, WhatsAppMessage.Status.READ],
        created_at__gt=inbound.created_at,
    ).exclude(raw_payload__has_any_keys=[
        "shvya_ai", "shvya_welcome", "shvya_auto_followup", "shvya_workflow",
    ]).order_by("-created_at", "-id").first()
    return human_reply or inbound


def _connected_hosted_account(*, account_id, organization_id):
    return (
        WhatsAppAccount.objects.filter(
            pk=account_id,
            organization_id=organization_id,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            is_active=True,
            status=WhatsAppAccount.Status.CONNECTED,
        )
        .first()
    )


def _has_existing_response(*, lead, account, inbound_message, body):
    outbound = WhatsAppMessage.objects.filter(
        organization_id=lead.organization_id,
        account=account,
        lead=lead,
        direction=WhatsAppMessage.Direction.OUTBOUND,
    )
    source_id = str(inbound_message.id)
    if outbound.filter(
        raw_payload__shvya_ai__source_inbound_message_id=source_id,
    ).exists():
        return True
    if not body:
        return False
    return outbound.filter(
        body=body,
        created_at__gte=inbound_message.created_at,
    ).exists()


def _queue_decision_message(*, organization, account, lead, decision, body):
    outbound_kwargs = {
        "organization": organization,
        "account": account,
        "to_number": lead.phone,
        "body": body,
        "lead": lead,
    }

    if decision.file_document_id is not None:
        try:
            document_id = int(decision.file_document_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("Engagement decision contains an invalid file_document_id.") from exc
        if document_id <= 0:
            raise ValueError("Engagement decision contains an invalid file_document_id.")

        from apps.ai_engagement.services.file_sharing import FileSharingService
        eligible_files = FileSharingService.eligible_documents(organization=organization)
        if eligible_files.exclude(share_instruction="").exists():
            eligible_files = eligible_files.exclude(share_instruction="")
        if not eligible_files.filter(id=document_id).exists():
            raise ValueError(
                "Engagement decision selected a file that is not configured for AI-guided sharing."
            )

        outbound_kwargs.update(
            {
                "message_type": WhatsAppMessage.MessageType.DOCUMENT,
                "media_payload": {
                    "source": "document",
                    "document_id": document_id,
                },
            }
        )

    return queue_outbound_message(**outbound_kwargs)


def execute_hosted_ai_engagement(*, task, job):
    """Run one Hosted AI turn using only ``job.account`` and ``job.source_message``.

    The returned payload matches the canonical AI task contract.  Delivery is
    intentionally not dispatched here; ``process_hosted_ai_engagement_job_task``
    owns Hosted Account Health pacing and sends the exact queued message.
    """
    from apps.ai_engagement.tasks import (
        _persist_engagement_answers,
        _whatsapp_send_eligible,
    )

    try:
        lead = (
            Lead.objects.select_related("organization", "pipeline", "stage")
            .get(pk=job.lead_id, organization_id=job.organization_id)
        )
    except Lead.DoesNotExist:
        return {"status": "skipped", "reason": "lead_not_found", "lead_id": str(job.lead_id)}

    organization = lead.organization
    account = _connected_hosted_account(
        account_id=job.account_id,
        organization_id=organization.id,
    )
    if account is None:
        return {
            "status": "skipped",
            "reason": "no_connected_whatsapp_account",
            "lead_id": str(lead.id),
        }

    source = (
        WhatsAppMessage.objects.select_related("account")
        .filter(
            pk=job.source_message_id,
            organization=organization,
            account=account,
            lead=lead,
            direction=WhatsAppMessage.Direction.INBOUND,
        )
        .first()
    )
    if source is None:
        return {
            "status": "skipped",
            "reason": "source_message_missing",
            "lead_id": str(lead.id),
        }

    # A redelivery must not pay for a second model turn while its first reply
    # is waiting for the shared send slot. The job worker recovers and sends
    # that exact row; direct execution callers simply observe the duplicate.
    if _has_existing_response(lead=lead, account=account, inbound_message=source, body=""):
        return {"status": "skipped", "reason": "duplicate_ai_response",
                "lead_id": str(lead.pk), "source_message_id": str(source.pk)}

    latest = _latest_for_account(lead=lead, account=account)
    if latest is None or latest.id != source.id or latest.direction != WhatsAppMessage.Direction.INBOUND:
        return {
            "status": "skipped",
            "reason": "conversation_changed_before_generation",
            "lead_id": str(lead.id),
            "source_message_id": str(source.id),
        }

    try:
        permission = AIPermissionService().evaluate(
            organization=organization,
            lead=lead,
            latest_inbound=source,
        )
    except AIPermissionError as exc:
        logger.exception("Hosted AI permission evaluation failed for lead %s", lead.id)
        raise task.retry(exc=exc, countdown=30)
    if not permission.allowed:
        return {"status": "skipped", "reason": permission.reason, "lead_id": str(lead.id)}

    service = EngagementService(
        context_builder=HostedAIContextBuilder(account_id=account.id),
    )
    try:
        decision = service.engage(organization=organization, lead=lead)
    except EngagementError as exc:
        provider_error = exc.__cause__
        if isinstance(provider_error, AIProviderTransientError):
            raise task.retry(
                exc=provider_error,
                countdown=provider_retry_countdown(
                    provider_error,
                    identifier=lead.id,
                    retries=task.request.retries,
                    default=30,
                ),
            )
        logger.error(
            "Hosted AI engagement permanently failed for lead %s; using deterministic fail-soft",
            lead.id,
        )
        decision = build_deterministic_fallback_decision(
            organization=organization,
            lead=lead,
            latest_inbound=source,
        )
    except AIProviderTransientError as exc:
        raise task.retry(
            exc=exc,
            countdown=provider_retry_countdown(
                exc,
                identifier=lead.id,
                retries=task.request.retries,
                default=30,
            ),
        )
    except Exception as exc:
        logger.exception("Unexpected Hosted AI generation failure for lead %s", lead.id)
        raise task.retry(exc=exc)

    try:
        with transaction.atomic():
            from apps.hosted_automation.models import HostedAutomationJob
            if job.claim_token and not HostedAutomationJob.objects.filter(
                pk=job.pk, status=HostedAutomationJob.Status.PROCESSING,
                claim_token=job.claim_token,
            ).exists():
                return {"status": "skipped", "reason": "job_lease_replaced"}
            locked_lead = (
                Lead.objects.select_for_update()
                .select_related("organization", "pipeline", "stage")
                .get(pk=lead.pk)
            )
            locked_account = _connected_hosted_account(
                account_id=account.id,
                organization_id=organization.id,
            )
            if locked_account is None:
                return {
                    "status": "skipped",
                    "reason": "no_connected_whatsapp_account",
                    "lead_id": str(lead.id),
                }

            latest = _latest_for_account(lead=locked_lead, account=locked_account)
            if latest is None or latest.id != source.id or latest.direction != WhatsAppMessage.Direction.INBOUND:
                return {
                    "status": "skipped",
                    "reason": "conversation_changed_before_send",
                    "lead_id": str(lead.id),
                    "source_message_id": str(source.id),
                }

            permission = AIPermissionService().evaluate(
                organization=organization,
                lead=locked_lead,
                latest_inbound=latest,
            )
            if not permission.allowed:
                return {
                    "status": "skipped",
                    "reason": permission.reason,
                    "lead_id": str(lead.id),
                }

            if not decision.should_engage:
                if not _persist_engagement_answers(locked_lead, decision, source.id):
                    return {
                        "status": "skipped",
                        "reason": "message_already_processed",
                        "lead_id": str(lead.id),
                    }
                crm_result = CRMActionExecutor().execute(
                    organization=organization,
                    lead=locked_lead,
                    actions=decision.crm_actions,
                )
                return {
                    "status": "completed",
                    "reason": "no_engagement",
                    "lead_id": str(lead.id),
                    "crm": crm_result,
                    "source_message_id": str(source.id),
                    "model": decision.model,
                }

            body = str(decision.message or "").strip()
            if not body:
                return {
                    "status": "failed",
                    "reason": "empty_engagement_message",
                    "lead_id": str(lead.id),
                }

            if _has_existing_response(
                lead=locked_lead,
                account=locked_account,
                inbound_message=latest,
                body=body,
            ):
                return {
                    "status": "skipped",
                    "reason": "duplicate_ai_response",
                    "lead_id": str(lead.id),
                    "source_message_id": str(source.id),
                }

            eligible, eligibility_reason = _whatsapp_send_eligible(
                lead=locked_lead,
                inbound_message=latest,
                account=locked_account,
            )
            if not eligible:
                return {
                    "status": "skipped",
                    "reason": eligibility_reason,
                    "lead_id": str(lead.id),
                    "source_message_id": str(source.id),
                }

            if not _persist_engagement_answers(locked_lead, decision, source.id):
                return {
                    "status": "skipped",
                    "reason": "message_already_processed",
                    "lead_id": str(lead.id),
                }

            crm_result = CRMActionExecutor().execute(
                organization=organization,
                lead=locked_lead,
                actions=decision.crm_actions,
            )
            outbound = _queue_decision_message(
                organization=organization,
                account=locked_account,
                lead=locked_lead,
                decision=decision,
                body=body,
            )
            outbound.raw_payload = {
                "shvya_ai": {
                    "job_id": str(job.pk),
                    "source_inbound_message_id": str(source.id),
                    "queued_at": source.created_at.isoformat(),
                    "model": decision.model,
                    "reason": decision.reason,
                    "next_requirement_id": decision.next_requirement_id,
                    "provider": "hosted",
                }
            }
            outbound.save(update_fields=["raw_payload", "updated_at"])

    except (AIPermissionError, CRMActionExecutionError) as exc:
        logger.exception("Hosted AI finalization failed for lead %s", lead.id)
        raise task.retry(exc=exc, countdown=30)
    except Exception as exc:
        logger.exception("Unexpected Hosted AI finalization failure for lead %s", lead.id)
        raise task.retry(exc=exc)

    return {
        "status": "completed",
        "lead_id": str(lead.id),
        "engaged": True,
        "crm": crm_result,
        "message_id": str(outbound.id),
        "source_message_id": str(source.id),
        "model": decision.model,
    }
