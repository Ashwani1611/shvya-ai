"""Worker-boundary recovery for permanent customer engagement generation failures.

The canonical engagement task remains authoritative. This module intercepts only
its terminal ``engagement_generation_failed`` result and finalizes a deterministic
provider-free reply through the same permission, freshness, idempotency and
WhatsApp queue checks. Temporary/provider retry failures are never converted.
"""

from __future__ import annotations

import logging

from django.db import transaction


logger = logging.getLogger(__name__)
_INSTALLED = False


def _finalize_terminal_generation_failure(*, task, lead_id: str):
    from apps.ai_engagement import tasks as task_module
    from apps.ai_engagement.services.ai_permissions import (
        AIPermissionError,
        AIPermissionService,
    )
    from apps.ai_engagement.services.crm_executor import (
        CRMActionExecutionError,
        CRMActionExecutor,
    )
    from apps.ai_engagement.services.engagement_failsoft import (
        build_deterministic_fallback_decision,
    )
    from apps.channels.models import WhatsAppMessage
    from apps.channels.tasks import send_whatsapp_message_task
    from apps.crm.models import Lead
    from services.channels.whatsapp_service import (
        queue_outbound_message,
        resolve_account_for_lead,
    )

    try:
        lead = (
            Lead.objects.select_related("organization", "pipeline", "stage")
            .get(id=lead_id)
        )
    except Lead.DoesNotExist:
        return {
            "status": "skipped",
            "reason": "lead_not_found",
            "lead_id": str(lead_id),
        }

    organization = lead.organization

    try:
        permission = AIPermissionService().evaluate(
            organization=organization,
            lead=lead,
        )
    except AIPermissionError as exc:
        logger.exception(
            "Deterministic engagement fail-soft permission check failed for lead %s",
            lead_id,
        )
        raise task.retry(exc=exc, countdown=30)

    if not permission.allowed:
        return {
            "status": "skipped",
            "reason": permission.reason,
            "lead_id": str(lead_id),
        }

    account = resolve_account_for_lead(
        organization=organization,
        lead=lead,
    )
    if account is None:
        return {
            "status": "skipped",
            "reason": "no_connected_whatsapp_account",
            "lead_id": str(lead_id),
        }

    source = task_module._latest_whatsapp_message(lead=lead)
    if source is None:
        return {
            "status": "skipped",
            "reason": "no_whatsapp_messages",
            "lead_id": str(lead_id),
        }
    if source.direction != WhatsAppMessage.Direction.INBOUND:
        return {
            "status": "skipped",
            "reason": "latest_message_not_inbound",
            "lead_id": str(lead_id),
            "latest_message_id": str(source.id),
        }

    source_message_id = source.id

    try:
        decision = build_deterministic_fallback_decision(
            organization=organization,
            lead=lead,
        )
    except Exception as exc:
        logger.exception(
            "Deterministic engagement fail-soft construction failed for lead %s",
            lead_id,
        )
        raise task.retry(exc=exc, countdown=30)

    # The fallback is deliberately side-effect free. Reuse the canonical final
    # transaction so pause/opt-out, latest-message changes and duplicate replies
    # remain authoritative even after the original generation failure.
    try:
        with transaction.atomic():
            locked_lead = (
                Lead.objects.select_for_update()
                .select_related("organization", "pipeline", "stage")
                .get(id=lead_id)
            )

            latest = task_module._latest_whatsapp_message(lead=locked_lead)
            if (
                latest is None
                or latest.id != source_message_id
                or latest.direction != WhatsAppMessage.Direction.INBOUND
            ):
                return {
                    "status": "skipped",
                    "reason": "conversation_changed_before_send",
                    "lead_id": str(lead_id),
                    "source_message_id": str(source_message_id),
                }

            permission = AIPermissionService().evaluate(
                organization=organization,
                lead=locked_lead,
            )
            if not permission.allowed:
                return {
                    "status": "skipped",
                    "reason": permission.reason,
                    "lead_id": str(lead_id),
                }

            account = resolve_account_for_lead(
                organization=organization,
                lead=locked_lead,
            )
            if account is None:
                return {
                    "status": "skipped",
                    "reason": "no_connected_whatsapp_account",
                    "lead_id": str(lead_id),
                }

            if not decision.should_engage:
                if not task_module._persist_engagement_answers(
                    locked_lead,
                    decision,
                    source_message_id,
                ):
                    return {
                        "status": "skipped",
                        "reason": "message_already_processed",
                        "lead_id": str(lead_id),
                    }
                crm_result = CRMActionExecutor().execute(
                    organization=organization,
                    lead=locked_lead,
                    actions=[],
                )
                return {
                    "status": "completed",
                    "reason": "no_engagement",
                    "lead_id": str(lead_id),
                    "crm": crm_result,
                    "model": decision.model,
                    "source_message_id": str(source_message_id),
                }

            body = str(decision.message or "").strip()
            if not body:
                raise ValueError("Deterministic fail-soft produced an empty customer reply.")

            if task_module._has_existing_ai_response(
                lead=locked_lead,
                inbound_message=latest,
                body=body,
            ):
                return {
                    "status": "skipped",
                    "reason": "duplicate_ai_response",
                    "lead_id": str(lead_id),
                    "source_message_id": str(source_message_id),
                }

            send_eligible, eligibility_reason = task_module._whatsapp_send_eligible(
                lead=locked_lead,
                inbound_message=latest,
                account=account,
            )
            if not send_eligible:
                return {
                    "status": "skipped",
                    "reason": eligibility_reason,
                    "lead_id": str(lead_id),
                    "source_message_id": str(source_message_id),
                }

            if not task_module._persist_engagement_answers(
                locked_lead,
                decision,
                source_message_id,
            ):
                return {
                    "status": "skipped",
                    "reason": "message_already_processed",
                    "lead_id": str(lead_id),
                }

            crm_result = CRMActionExecutor().execute(
                organization=organization,
                lead=locked_lead,
                actions=[],
            )

            outbound = queue_outbound_message(
                organization=organization,
                account=account,
                to_number=locked_lead.phone,
                body=body,
                lead=locked_lead,
            )
            outbound.raw_payload = {
                "shvya_ai": {
                    "source_inbound_message_id": str(source_message_id),
                    "model": decision.model,
                    "reason": decision.reason,
                    "next_requirement_id": decision.next_requirement_id,
                    "failsoft": True,
                }
            }
            outbound.save(update_fields=["raw_payload", "updated_at"])

            transaction.on_commit(
                lambda message_id=str(outbound.id): send_whatsapp_message_task.delay(
                    message_id
                )
            )

    except (AIPermissionError, CRMActionExecutionError) as exc:
        logger.exception(
            "Deterministic engagement fail-soft finalization failed for lead %s",
            lead_id,
        )
        raise task.retry(exc=exc, countdown=30)
    except Exception as exc:
        logger.exception(
            "Unexpected deterministic engagement fail-soft failure for lead %s",
            lead_id,
        )
        raise task.retry(exc=exc, countdown=30)

    return {
        "status": "completed",
        "lead_id": str(lead_id),
        "engaged": True,
        "crm": crm_result,
        "message_id": str(outbound.id),
        "source_message_id": str(source_message_id),
        "model": decision.model,
        "reason": "deterministic_failsoft",
    }


def install_task_execution_failsoft() -> None:
    """Guarantee a safe reply when the canonical task returns terminal generation failure."""
    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement import tasks as task_module

    original = task_module._execute_ai_engagement_response_impl

    def guarded(*, task, lead_id: str):
        result = original(task=task, lead_id=lead_id)
        if str((result or {}).get("reason") or "") != "engagement_generation_failed":
            return result

        logger.error(
            "Canonical engagement generation permanently failed for lead %s; "
            "finalizing deterministic fail-soft response",
            lead_id,
        )
        return _finalize_terminal_generation_failure(
            task=task,
            lead_id=lead_id,
        )

    task_module._execute_ai_engagement_response_impl = guarded
    _INSTALLED = True
