"""Phone-independent Instagram AI engagement.

A live Instagram DM is identified by Meta's participant/conversation identity.
This executor reuses SHVYA's AI Brain / playbook engine and CRM action policy
without requiring a WhatsApp number.
"""
from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import replace

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
from apps.ai_engagement.services.instagram_trace import traced_instagram_turn
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

    def _get_messages(self, *, organization, lead, limit, through_message=None):
        query = InstagramMessage.objects.filter(
            organization=organization,
            conversation_id=self.conversation_id,
            conversation__lead=lead,
        )
        if through_message is not None:
            query = query.filter(account_id=through_message.account_id).filter(
                Q(created_at__lt=through_message.created_at)
                | Q(created_at=through_message.created_at, pk__lte=through_message.pk)
            )
        messages = list(
            query
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
        return {"message_count": len(normalized), "messages": normalized, "channel": "instagram", "id": str(self.conversation_id), "execution_mode": "live"}

    def _build_lead_context(self, *, lead):
        context = super()._build_lead_context(lead=lead)
        # Count provider-accepted/read files only in this exact conversation.
        # The inherited runtime history belongs to WhatsApp delivery. A file
        # sent there (or in another Instagram thread) has not been delivered in
        # this conversation and must remain available to Instagram's playbook.
        shared_ids = set()
        document_ids = InstagramMessage.objects.filter(
            organization_id=lead.organization_id,
            conversation_id=self.conversation_id,
            conversation__lead=lead,
            direction=InstagramMessage.Direction.OUTBOUND,
            status__in=[InstagramMessage.Status.SENT, InstagramMessage.Status.READ],
            raw_payload__shvya_ai__file_document_id__isnull=False,
        ).order_by().values_list("raw_payload__shvya_ai__file_document_id", flat=True).distinct()
        for value in document_ids:
            if not isinstance(value, bool) and str(value or "").isdigit():
                shared_ids.add(int(value))
        context["shared_document_ids"] = sorted(shared_ids)
        return context

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
    existing = list(
        conversation.messages.filter(
            organization_id=conversation.organization_id,
            account_id=conversation.account_id,
            direction=InstagramMessage.Direction.OUTBOUND,
            raw_payload__shvya_ai__source_inbound_message_id=str(source_message_id),
        ).order_by("created_at", "id")
    )
    if not existing:
        return None

    queued = [item for item in existing if item.status == InstagramMessage.Status.QUEUED]
    try:
        for item in queued:
            _dispatch_instagram_ai_message(item.pk)
    except Exception as exc:
        raise task.retry(exc=exc, countdown=20)
    return {
        "status": "queued" if queued else "skipped",
        "reason": "existing_ai_response_requeued" if queued else "duplicate_ai_response",
        "lead_id": str(conversation.lead_id) if conversation.lead_id else None,
        "source_message_id": str(source_message_id),
        "message_id": str(existing[0].pk),
        "message_ids": [str(item.pk) for item in existing],
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
    processing = payload.get("shvya_ai_processing")
    payload["shvya_ai_processing"] = {
        **(processing if isinstance(processing, dict) else {}),
        "message_id": str(source.pk),
        "processed": True,
        "response_hash": response_hash(getattr(decision, "message", "")),
    }
    source.raw_payload = payload
    source.save(update_fields=["raw_payload", "updated_at"])


def _source_state_resolved(source):
    payload = source.raw_payload if isinstance(source.raw_payload, dict) else {}
    processing = payload.get("shvya_ai_processing")
    return bool(
        isinstance(processing, dict)
        and processing.get("state_resolved")
        and str(processing.get("message_id") or "") == str(source.pk)
    )


def _finalize_decision_state(*, organization, lead, source, decision):
    from apps.ai_engagement.services.qualification_state import record_last_asked_requirement, state_for_lead
    from apps.ai_engagement.services.runtime_state import finalize_runtime
    from apps.ai_engagement.services.transactional_turn_runtime import _requirements_for_turn

    requirements = _requirements_for_turn(organization=organization, lead=lead)
    finalize_runtime(
        lead=lead, decision=decision,
        qualification=state_for_lead(lead, requirements=requirements),
        requirements=requirements, message_id=source.pk,
    )
    if getattr(decision, "next_requirement_id", None):
        record_last_asked_requirement(lead, decision.next_requirement_id, requirements=requirements)


def _apply_decision_state(*, organization, lead, source, decision, finalize=True):
    """Apply CRM/qualification state in the canonical deterministic order."""
    from apps.ai_engagement.services.qualification_state import (
        normalize_stage_name,
        persist_answer_updates,
        project_answer_updates,
        state_for_lead,
    )
    from apps.ai_engagement.services.transactional_turn_runtime import (
        _qualified_action,
        _requirements_for_turn,
        _split_actions,
    )
    from apps.ai_engagement.services.qualification_execution.actions import (
        answers_captured_for_source,
        completion_stage_actions,
        rebuild_mapped_attribute_actions,
    )
    from apps.ai_engagement.services.qualification_execution.config import _config
    from apps.ai_engagement.services.qualification_execution.completion import (
        _configured_completion_reminders,
    )

    if _source_state_resolved(source):
        if finalize:
            _finalize_decision_state(organization=organization, lead=lead, source=source, decision=decision)
        return []

    requirements = _requirements_for_turn(
        organization=organization,
        lead=lead,
    )
    before = state_for_lead(lead, requirements=requirements)
    updates_by_id = {
        str(item["requirement_id"]): item
        for item in answers_captured_for_source(state=before, source=source)
    }
    proposed_updates = getattr(decision, "qualification_updates", []) or []
    try:
        project_answer_updates(
            state=before, requirements=requirements, updates=proposed_updates,
            messages=[{"id": str(source.pk), "body": source.body, "direction": "inbound"}],
        )
    except ValueError as exc:
        raise CRMActionExecutionError("Invalid source-bound qualification updates.") from exc
    for item in proposed_updates:
        updates_by_id[str(item["requirement_id"])] = item
    updates = list(updates_by_id.values())
    config = _config(organization=organization, requirements=requirements)
    actions = rebuild_mapped_attribute_actions(
        actions=getattr(decision, "crm_actions", []) or [], updates=updates,
        requirements=requirements, config=config,
    )
    attribute_actions, proposed_stage_actions, other_actions = _split_actions(
        actions
    )
    from types import SimpleNamespace
    from apps.ai_engagement.services.crm_routing_reliability import _ensure_datetime_reminder

    builder = InstagramAIContextBuilder(conversation_id=source.conversation_id)
    reminder_context = SimpleNamespace(
        organization={"timezone": organization.timezone},
        conversation=builder._build_conversation_context(messages=builder._get_messages(
            organization=organization, lead=lead, limit=12, through_message=source,
        )),
    )
    _ensure_datetime_reminder(
        other_actions, source.body,
        {"crm": {"reminders": config.get("reminder_rules") or []}},
        context=reminder_context,
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
    if completed_in_new_lead and updates and not any(
        action.get("type") == "create_reminder" for action in other_actions
    ):
        other_actions.extend(_configured_completion_reminders(config))
    stage_actions = completion_stage_actions(
        organization=organization, lead=lead, source=source, proposed=proposed_stage_actions,
        completion=completion_action, config=config,
    ) if completed_in_new_lead else proposed_stage_actions[:1]
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

    if finalize:
        _finalize_decision_state(organization=organization, lead=lead, source=source, decision=decision)
    return results


def _resolve_instagram_state(*, organization, lead, source, decision, revision):
    """Commit source-bound CRM effects before composing the actual reply."""
    from apps.ai_engagement.services.file_sharing import FileSharingError, FileSharingService
    from apps.ai_engagement.services.runtime_state import state_revision
    from apps.ai_engagement.services.transactional_decision_reuse import _PENDING_FILE_RESOLUTION
    from apps.ai_engagement.services.transactional_turn_runtime import _mark_state_resolved
    from apps.ai_engagement.services.qualification_execution.actions import answers_captured_for_source
    from apps.ai_engagement.services.qualification_state import state_for_lead

    document_id = getattr(decision, "file_document_id", None)
    if document_id is not None and FileSharingService().get_guided_document(
        organization=organization, document_id=document_id,
    ) is None:
        raise FileSharingError("The selected file is no longer available.")
    with transaction.atomic():
        conversation = InstagramConversation.objects.select_for_update(of=("self",)).get(
            pk=source.conversation_id, organization=organization, account_id=source.account_id,
        )
        locked_lead = Lead.objects.select_for_update().select_related("stage", "pipeline").get(
            pk=lead.pk, organization=organization,
        )
        inbound = InstagramMessage.objects.select_for_update().get(
            pk=source.pk, organization=organization, account_id=source.account_id,
            conversation=conversation, direction=InstagramMessage.Direction.INBOUND,
        )
        latest = _latest_customer_turn(conversation)
        if conversation.lead_id != locked_lead.pk or latest is None or latest.pk != inbound.pk:
            return {"reason": "conversation_changed_before_state_resolution"}
        if _source_processed(inbound) or _existing_ai_response(
            conversation=conversation, source_message_id=inbound.pk,
        ) is not None:
            return {"reason": "source_already_processed"}
        if _source_state_resolved(inbound):
            return {"resolved": True, "results": []}
        if revision != state_revision(locked_lead):
            raise CRMActionExecutionError("Lead state changed during Instagram generation; retry required.")
        permission = AIPermissionService().evaluate(organization=organization, lead=locked_lead, channel="instagram")
        if not permission.allowed:
            return {"reason": permission.reason}
        if decision.should_engage:
            from services.channels.instagram_inbox import assert_reply_allowed

            assert_reply_allowed(conversation)
        results = _apply_decision_state(
            organization=organization, lead=locked_lead, source=inbound, decision=decision, finalize=False,
        )
        action_types = [
            str(result.get("type") or "") for result in results
            if isinstance(result, dict) and result.get("status") == "executed"
        ]
        if getattr(decision, "qualification_updates", []) or answers_captured_for_source(
            state=state_for_lead(locked_lead), source=inbound,
        ):
            action_types.append("qualification_state")
        token = _PENDING_FILE_RESOLUTION.set({
            "lead_id": str(lead.pk), "source_message_id": str(source.pk), "document_id": document_id,
        })
        try:
            _mark_state_resolved(lead=locked_lead, inbound=inbound, action_types=action_types)
        finally:
            _PENDING_FILE_RESOLUTION.reset(token)
    return {"resolved": True, "results": results}


def _generate_instagram_decision(*, service, organization, lead, source):
    from apps.ai_engagement.services.phase5_6_runtime import source_evidence_context
    from apps.ai_engagement.services.post_state_finalization_guard import _FINAL_LANGUAGE_ONLY

    resolved = _source_state_resolved(source)
    token = _FINAL_LANGUAGE_ONLY.set(resolved)
    try:
        with source_evidence_context(organization=organization, lead=lead, source=source, provider=service.provider):
            decision = service.engage(organization=organization, lead=lead)
    finally:
        _FINAL_LANGUAGE_ONLY.reset(token)
    if resolved:
        processing = source.raw_payload["shvya_ai_processing"]
        decision = replace(
            decision, crm_actions=[], qualification_updates=[],
            file_document_id=processing.get("resolved_file_document_id"),
        )
    return decision


@traced_instagram_turn
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

    # Refresh the relation cached before automatic lead creation.
    source.conversation = conversation

    from apps.ai_engagement.services.turn_controller import TurnController

    service = TurnController(
        context_builder=InstagramAIContextBuilder(
            conversation_id=conversation.pk,
        ),
        service_class=EngagementService,
    )
    from apps.ai_engagement.services.runtime_state import state_revision
    from apps.ai_engagement.services.transactional_turn_runtime import _state_changing_decision

    state_results = []
    try:
        revision = state_revision(lead)
        decision = _generate_instagram_decision(
            service=service, organization=source.organization, lead=lead, source=source,
        )
        # The graph can persist an unambiguous answer while understanding this
        # turn. Its validated revision includes that own write, but does not
        # bless unrelated changes by refreshing the lead after provider I/O.
        revision = getattr(decision, "backend_revision", "") or revision
        from apps.ai_engagement.services.qualification_execution.actions import answers_captured_for_source
        from apps.ai_engagement.services.qualification_state import state_for_lead

        captured = answers_captured_for_source(state=state_for_lead(lead), source=source)
        if not _source_state_resolved(source) and (_state_changing_decision(decision) or captured):
            resolution = _resolve_instagram_state(
                organization=source.organization, lead=lead, source=source, decision=decision, revision=revision,
            )
            if not resolution.get("resolved"):
                return {"status": "skipped", "reason": resolution["reason"], "lead_id": str(lead.pk)}
            state_results = resolution["results"]
            lead.refresh_from_db()
            source.refresh_from_db()
            revision = state_revision(lead)
            # Provider work deliberately happens after the state transaction.
            # A retry resumes this language-only pass from the persisted marker.
            decision = _generate_instagram_decision(
                service=service, organization=source.organization, lead=lead, source=source,
            )
            revision = getattr(decision, "backend_revision", "") or revision
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

    # Fail-soft composition after a committed state pass remains language-only
    # and cannot replace the exact file selection already validated for this turn.
    if _source_state_resolved(source):
        decision = replace(
            decision, crm_actions=[], qualification_updates=[],
            file_document_id=source.raw_payload["shvya_ai_processing"].get("resolved_file_document_id"),
        )

    try:
        with transaction.atomic():
            locked_conversation = (
                InstagramConversation.objects.select_for_update(of=("self",))
                .select_related("account")
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

            if revision != state_revision(locked_lead):
                raise CRMActionExecutionError("Lead state changed before Instagram reply; retry required.")

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

            crm_results = state_results + _apply_decision_state(
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

            # Queue the file with the reply; retries republish the same rows.
            outbound_ids = [str(outbound.pk)]
            document_id = getattr(decision, "file_document_id", None)
            if document_id is not None:
                from apps.ai_engagement.services.instagram_files import queue_guided_file_reply

                file_message = queue_guided_file_reply(
                    organization=source.organization,
                    conversation=locked_conversation,
                    document_id=document_id,
                    ai_metadata=outbound.raw_payload["shvya_ai"],
                )
                outbound_ids.append(str(file_message.pk))

            outbound_id = str(outbound.pk)
            # Let broker publication failures propagate after the database
            # commit. Celery will retry this generation task; the retry sees the
            # already-queued AI outbound above and republishes only its send task.
            for queued_id in outbound_ids:
                transaction.on_commit(
                    lambda queued_id=queued_id: _dispatch_instagram_ai_message(queued_id),
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
        "message_ids": outbound_ids,
        "source_message_id": str(source.pk),
        "model": decision.model,
    }


def _dispatch_instagram_ai_message(message_id):
    from apps.channels.instagram_tasks import send_instagram_message_task

    send_instagram_message_task.delay(str(message_id))
