"""Phone-independent Instagram AI engagement.

A live Instagram DM is identified by Meta's participant/conversation identity.
This executor reuses SHVYA's AI Brain / playbook engine and CRM action policy
without requiring a WhatsApp number.
"""
from __future__ import annotations

import logging
from copy import deepcopy

from django.db import transaction
from django.db.models import Q

from apps.ai_engagement.services.ai_permissions import (
    AIPermissionError,
    AIPermissionService,
)
from apps.ai_engagement.services.ai_provider import (
    AIProviderTransientError,
    provider_retry_countdown,
)
from apps.ai_engagement.services.context import AIContextBuilder
from apps.ai_engagement.services.crm_executor import (
    CRMActionExecutionError,
    CRMActionExecutor,
)
from apps.ai_engagement.services.engagement import EngagementError, EngagementService
from apps.ai_engagement.services.engagement_failsoft import (
    build_deterministic_fallback_decision,
)
from apps.channels.instagram_models import InstagramConversation, InstagramMessage
from apps.crm.models import Lead


logger = logging.getLogger(__name__)


class InstagramAIContextBuilder(AIContextBuilder):
    """Build AI Brain context only from one exact Instagram conversation."""

    def __init__(self, *, conversation_id):
        self.conversation_id = conversation_id

    def _get_messages(self, *, organization, lead, limit):
        messages = list(
            InstagramMessage.objects.filter(
                organization=organization,
                conversation_id=self.conversation_id,
                conversation__lead=lead,
            )
            .exclude(
                Q(direction=InstagramMessage.Direction.OUTBOUND)
                & ~Q(
                    status__in=[
                        InstagramMessage.Status.SENT,
                        InstagramMessage.Status.READ,
                    ]
                )
            )
            .order_by("-created_at", "-id")[:limit]
        )
        messages.reverse()
        return messages

    def _build_conversation_context(self, *, messages):
        normalized = []
        for message in messages:
            body = str(message.body or "").strip()
            if not body and message.attachments:
                body = "[Instagram attachment]"
            if not body:
                continue
            normalized.append(
                {
                    "id": str(message.id),
                    "direction": message.direction,
                    "speaker": (
                        "lead"
                        if message.direction == InstagramMessage.Direction.INBOUND
                        else "shvya"
                    ),
                    "body": body,
                    "status": message.status,
                    "created_at": (
                        message.created_at.isoformat()
                        if message.created_at
                        else None
                    ),
                }
            )
        return {"message_count": len(normalized), "messages": normalized}

    def latest_inbound_for_fallback(self, *, organization, lead):
        return (
            InstagramMessage.objects.filter(
                organization=organization,
                conversation_id=self.conversation_id,
                conversation__lead=lead,
                direction=InstagramMessage.Direction.INBOUND,
            )
            .order_by("-created_at", "-id")
            .first()
        )


def _latest_customer_turn(conversation):
    inbound = (
        conversation.messages.filter(
            organization_id=conversation.organization_id,
            account_id=conversation.account_id,
            direction=InstagramMessage.Direction.INBOUND,
        )
        .order_by("-created_at", "-id")
        .first()
    )
    if inbound is None:
        return None

    # A human reply queued/sent after the customer's latest DM owns the turn.
    human_reply = (
        conversation.messages.filter(
            organization_id=conversation.organization_id,
            account_id=conversation.account_id,
            direction=InstagramMessage.Direction.OUTBOUND,
            created_at__gt=inbound.created_at,
        )
        .exclude(raw_payload__has_key="shvya_ai")
        .order_by("-created_at", "-id")
        .first()
    )
    return human_reply or inbound


def _existing_ai_response(*, conversation, source_message_id):
    return (
        conversation.messages.filter(
            organization_id=conversation.organization_id,
            account_id=conversation.account_id,
            direction=InstagramMessage.Direction.OUTBOUND,
            raw_payload__shvya_ai__source_inbound_message_id=str(source_message_id),
        )
        .order_by("-created_at", "-id")
        .first()
    )


def _resume_existing_ai_response(*, task, conversation, source_message_id):
    existing = _existing_ai_response(
        conversation=conversation,
        source_message_id=source_message_id,
    )
    if existing is None:
        return None

    if existing.status == InstagramMessage.Status.QUEUED:
        try:
            _dispatch_instagram_ai_message(existing.pk)
        except Exception as exc:
            raise task.retry(exc=exc, countdown=20)
        return {
            "status": "queued",
            "reason": "existing_ai_response_requeued",
            "lead_id": (
                str(conversation.lead_id)
                if conversation.lead_id
                else None
            ),
            "source_message_id": str(source_message_id),
            "message_id": str(existing.pk),
        }

    return {
        "status": "skipped",
        "reason": "duplicate_ai_response",
        "lead_id": (
            str(conversation.lead_id)
            if conversation.lead_id
            else None
        ),
        "source_message_id": str(source_message_id),
        "message_id": str(existing.pk),
    }


def _source_processed(source):
    payload = source.raw_payload if isinstance(source.raw_payload, dict) else {}
    processing = payload.get("shvya_ai_processing")
    return bool(
        isinstance(processing, dict)
        and processing.get("processed")
        and str(processing.get("message_id") or "") == str(source.pk)
    )


def _mark_source_processed(*, source, decision):
    from apps.ai_engagement.services.runtime_state import response_hash

    payload = deepcopy(source.raw_payload) if isinstance(source.raw_payload, dict) else {}
    payload["shvya_ai_processing"] = {
        "message_id": str(source.pk),
        "processed": True,
        "response_hash": response_hash(getattr(decision, "message", "")),
    }
    source.raw_payload = payload
    source.save(update_fields=["raw_payload", "updated_at"])


def _apply_decision_state(*, organization, lead, source, decision):
    """Apply CRM/qualification state in the canonical deterministic order."""
    from apps.ai_engagement.services.qualification_state import (
        normalize_stage_name,
        persist_answer_updates,
        record_last_asked_requirement,
        state_for_lead,
    )
    from apps.ai_engagement.services.runtime_state import finalize_runtime
    from apps.ai_engagement.services.transactional_turn_runtime import (
        _qualified_action,
        _requirements_for_turn,
        _split_actions,
    )

    requirements = _requirements_for_turn(
        organization=organization,
        lead=lead,
    )
    attribute_actions, proposed_stage_actions, other_actions = _split_actions(
        getattr(decision, "crm_actions", []) or []
    )
    executor = CRMActionExecutor()
    results = []

    if attribute_actions:
        results.extend(
            executor.execute(
                organization=organization,
                lead=lead,
                actions=attribute_actions,
                source_message=source,
            )
        )
        lead.refresh_from_db(fields=["attributes", "pipeline", "stage"])

    updates = getattr(decision, "qualification_updates", []) or []
    qualification = (
        persist_answer_updates(lead=lead, updates=updates)
        if updates
        else state_for_lead(lead, requirements=requirements)
    )
    lead.refresh_from_db(fields=["attributes", "pipeline", "stage"])
    qualification = state_for_lead(lead, requirements=requirements)

    completion_action = _qualified_action(
        lead=lead,
        qualification_state=qualification,
    )
    completed_in_new_lead = (
        str(qualification.get("qualification_status") or "").casefold() == "completed"
        and normalize_stage_name(getattr(lead.stage, "name", "")) == "new lead"
    )
    stage_actions = (
        [completion_action] if completed_in_new_lead and completion_action
        else ([] if completed_in_new_lead else proposed_stage_actions[:1])
    )
    if stage_actions:
        results.extend(
            executor.execute(
                organization=organization,
                lead=lead,
                actions=stage_actions,
                source_message=source,
            )
        )
        lead.refresh_from_db(fields=["attributes", "pipeline", "stage"])

    if other_actions:
        results.extend(
            executor.execute(
                organization=organization,
                lead=lead,
                actions=other_actions,
                source_message=source,
            )
        )
        lead.refresh_from_db(fields=["attributes", "pipeline", "stage"])

    qualification = state_for_lead(lead, requirements=requirements)
    finalize_runtime(
        lead=lead,
        decision=decision,
        qualification=qualification,
        requirements=requirements,
        message_id=source.pk,
    )

    if getattr(decision, "next_requirement_id", None):
        record_last_asked_requirement(
            lead,
            decision.next_requirement_id,
            requirements=requirements,
        )
    return results


def execute_instagram_ai_engagement(*, task, message_id):
    """Generate, finalize, queue and dispatch one Instagram AI reply."""
    source = (
        InstagramMessage.objects.select_related(
            "organization",
            "account",
            "conversation",
            "conversation__lead",
            "conversation__lead__pipeline",
            "conversation__lead__stage",
        )
        .filter(
            pk=message_id,
            direction=InstagramMessage.Direction.INBOUND,
        )
        .first()
    )
    if source is None:
        return {"status": "skipped", "reason": "source_message_missing"}

    conversation = source.conversation
    lead = conversation.lead
    if lead is None:
        from services.channels.instagram_leads import ensure_instagram_lead

        conversation, lead = ensure_instagram_lead(conversation_id=conversation.pk)
        if lead is None:
            return {
                "status": "skipped",
                "reason": "no_active_pipeline_stage",
                "source_message_id": str(source.pk),
            }

    existing_result = _resume_existing_ai_response(
        task=task,
        conversation=conversation,
        source_message_id=source.pk,
    )
    if existing_result is not None:
        return existing_result
    if _source_processed(source):
        return {
            "status": "skipped",
            "reason": "source_already_processed",
            "lead_id": str(lead.pk),
            "source_message_id": str(source.pk),
        }

    latest = _latest_customer_turn(conversation)
    if latest is None or latest.pk != source.pk or latest.direction != InstagramMessage.Direction.INBOUND:
        return {
            "status": "skipped",
            "reason": "conversation_changed_before_generation",
            "lead_id": str(lead.pk),
            "source_message_id": str(source.pk),
        }

    try:
        permission = AIPermissionService().evaluate(
            organization=source.organization,
            lead=lead,
            channel="instagram",
        )
    except AIPermissionError as exc:
        raise task.retry(exc=exc, countdown=30)
    if not permission.allowed:
        return {
            "status": "skipped",
            "reason": permission.reason,
            "lead_id": str(lead.pk),
        }

    service = EngagementService(
        context_builder=InstagramAIContextBuilder(
            conversation_id=conversation.pk,
        )
    )
    try:
        decision = service.engage(
            organization=source.organization,
            lead=lead,
        )
    except EngagementError as exc:
        provider_error = exc.__cause__
        if isinstance(provider_error, AIProviderTransientError):
            raise task.retry(
                exc=provider_error,
                countdown=provider_retry_countdown(
                    provider_error,
                    identifier=lead.pk,
                    retries=task.request.retries,
                    default=30,
                ),
            )
        logger.exception(
            "Instagram AI generation failed for lead %s; using fail-soft reply",
            lead.pk,
        )
        decision = build_deterministic_fallback_decision(
            organization=source.organization,
            lead=lead,
            latest_inbound=source,
        )
    except AIProviderTransientError as exc:
        raise task.retry(
            exc=exc,
            countdown=provider_retry_countdown(
                exc,
                identifier=lead.pk,
                retries=task.request.retries,
                default=30,
            ),
        )
    except Exception as exc:
        logger.exception("Unexpected Instagram AI generation failure for lead %s", lead.pk)
        raise task.retry(exc=exc)

    try:
        with transaction.atomic():
            locked_conversation = (
                InstagramConversation.objects.select_for_update()
                .select_related("account", "lead")
                .get(
                    pk=conversation.pk,
                    organization=source.organization,
                    account=source.account,
                )
            )
            locked_lead = (
                Lead.objects.select_for_update()
                .select_related("organization", "pipeline", "stage")
                .get(
                    pk=lead.pk,
                    organization=source.organization,
                )
            )
            locked_source = (
                InstagramMessage.objects.select_for_update()
                .get(
                    pk=source.pk,
                    organization=source.organization,
                    account=source.account,
                    conversation=locked_conversation,
                    direction=InstagramMessage.Direction.INBOUND,
                )
            )
            if locked_conversation.lead_id != locked_lead.pk:
                return {
                    "status": "skipped",
                    "reason": "conversation_lead_changed",
                    "lead_id": str(lead.pk),
                }

            latest = _latest_customer_turn(locked_conversation)
            if (
                latest is None
                or latest.pk != locked_source.pk
                or latest.direction != InstagramMessage.Direction.INBOUND
            ):
                return {
                    "status": "skipped",
                    "reason": "conversation_changed_before_send",
                    "lead_id": str(lead.pk),
                    "source_message_id": str(source.pk),
                }

            permission = AIPermissionService().evaluate(
                organization=source.organization,
                lead=locked_lead,
                channel="instagram",
            )
            if not permission.allowed:
                return {
                    "status": "skipped",
                    "reason": permission.reason,
                    "lead_id": str(lead.pk),
                }
            existing = _existing_ai_response(
                conversation=locked_conversation,
                source_message_id=locked_source.pk,
            )
            if existing is not None or _source_processed(locked_source):
                return {
                    "status": "skipped",
                    "reason": (
                        "duplicate_ai_response"
                        if existing is not None
                        else "source_already_processed"
                    ),
                    "lead_id": str(lead.pk),
                    "source_message_id": str(source.pk),
                }

            body = str(getattr(decision, "message", "") or "").strip()
            if decision.should_engage and not body:
                return {
                    "status": "failed",
                    "reason": "empty_engagement_message",
                    "lead_id": str(lead.pk),
                }

            if decision.should_engage:
                from services.channels.instagram_inbox import assert_reply_allowed

                assert_reply_allowed(locked_conversation)

            crm_results = _apply_decision_state(
                organization=source.organization,
                lead=locked_lead,
                source=locked_source,
                decision=decision,
            )
            _mark_source_processed(source=locked_source, decision=decision)

            if not decision.should_engage:
                return {
                    "status": "completed",
                    "reason": "no_engagement",
                    "lead_id": str(lead.pk),
                    "crm": crm_results,
                    "source_message_id": str(source.pk),
                    "model": decision.model,
                }

            from services.channels.instagram_inbox import queue_inbox_reply

            outbound = queue_inbox_reply(
                source.organization,
                conversation_id=locked_conversation.pk,
                body=body,
            )
            outbound.raw_payload = {
                **(outbound.raw_payload if isinstance(outbound.raw_payload, dict) else {}),
                "shvya_ai": {
                    "source_inbound_message_id": str(source.pk),
                    "model": decision.model,
                    "reason": decision.reason,
                    "next_requirement_id": decision.next_requirement_id,
                    "provider": "instagram",
                },
            }
            outbound.save(update_fields=["raw_payload", "updated_at"])

            outbound_id = str(outbound.pk)
            # Let broker publication failures propagate after the database
            # commit. Celery will retry this generation task; the retry sees the
            # already-queued AI outbound above and republishes only its send task.
            transaction.on_commit(
                lambda outbound_id=outbound_id: _dispatch_instagram_ai_message(outbound_id),
            )

    except (AIPermissionError, CRMActionExecutionError) as exc:
        logger.exception("Instagram AI finalization failed for lead %s", lead.pk)
        raise task.retry(exc=exc, countdown=30)
    except Exception as exc:
        logger.exception("Unexpected Instagram AI finalization failure for lead %s", lead.pk)
        raise task.retry(exc=exc)

    return {
        "status": "completed",
        "lead_id": str(lead.pk),
        "engaged": True,
        "crm": crm_results,
        "message_id": outbound_id,
        "source_message_id": str(source.pk),
        "model": decision.model,
    }


def _dispatch_instagram_ai_message(message_id):
    from apps.channels.instagram_tasks import send_instagram_message_task

    send_instagram_message_task.delay(str(message_id))
