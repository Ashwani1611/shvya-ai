"""Heavy real-time engagement execution used by the thin Celery task boundary."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.ai_engagement.services.ai_provider import (
    AIProviderTransientError,
    provider_retry_countdown,
)


logger = logging.getLogger(__name__)


def _persist_engagement_answers(lead, decision, source_message_id):
    """Revalidate and finalize once, inside the caller's lead transaction."""
    from apps.ai_engagement.models import OrgInfo
    from apps.ai_engagement.services.qualification_state import persist_answer_updates, state_for_lead, project_answer_updates, requirements_for_lead
    from apps.ai_engagement.services.organization_profile import compile_qualification_requirements
    from apps.ai_engagement.services.runtime_state import contract, validate_response, finalize_runtime, STATE_KEY, response_hash, observe_message, state_revision

    inbound = lead.whatsapp_messages.select_for_update().get(
        pk=source_message_id, organization_id=lead.organization_id, direction="inbound")
    payload = dict(inbound.raw_payload or {})
    if (payload.get("shvya_ai_processing") or {}).get("processed"):
        return False
    org_info = OrgInfo.objects.filter(organization_id=lead.organization_id).first()
    from apps.ai_engagement.services.playbook import qualification_questions
    requirements = compile_qualification_requirements(qualification_questions(org_info.ai_playbook if org_info else ""))["requirements"]
    requirements = requirements_for_lead(lead, requirements)
    if getattr(decision, "backend_revision", "") and decision.backend_revision != state_revision(lead):
        raise ValueError("Backend state changed during response generation; retry required.")
    if getattr(decision, "flow_version", "") and decision.flow_version != contract(qualification={}, requirements=requirements)["flow_version"]:
        raise ValueError("Qualification flow changed during response generation; retry required.")
    lead.attributes = dict(lead.attributes or {})
    lead.attributes[STATE_KEY] = observe_message(lead.attributes.get(STATE_KEY), inbound.body)
    projected = project_answer_updates(state=state_for_lead(lead, requirements=requirements),
        requirements=requirements, updates=getattr(decision, "qualification_updates", []),
        messages=[{"id": str(inbound.id), "body": inbound.body, "direction": "inbound"}])
    validate_response(decision=decision, requirements=requirements,
        runtime=contract(qualification=projected, requirements=requirements,
                         saved=(lead.attributes or {}).get(STATE_KEY), organization_id=lead.organization_id))
    persist_answer_updates(lead=lead, updates=getattr(decision, "qualification_updates", []))
    # The answer persistence wrapper reloads the row. Apply this message's
    # runtime intent afterwards so that reload cannot erase pause/resume/opt-out.
    lead.attributes = dict(lead.attributes or {})
    lead.attributes[STATE_KEY] = observe_message(lead.attributes.get(STATE_KEY), inbound.body)
    finalize_runtime(lead=lead, decision=decision, qualification=projected,
                     requirements=requirements, message_id=source_message_id)
    payload["shvya_ai_processing"] = {"message_id": str(source_message_id),
        "processed": True, "response_hash": response_hash(decision.message)}
    inbound.raw_payload = payload
    inbound.save(update_fields=["raw_payload", "updated_at"])
    return True


# ============================================================
# WHATSAPP AI ENGAGEMENT
# ============================================================


def _latest_whatsapp_message(*, lead):
    """Find the newest customer turn, preserving an actual human takeover.

    Outbound welcome/reply drafts are queue state, not a customer response.
    They must not hide an inbound turn while waiting for their send slot. Only
    a successfully sent human reply on the same conversation supersedes it.
    Source-bound AI duplicate protection remains in the execution finalizer.
    """
    from apps.channels.models import WhatsAppMessage

    messages = lead.whatsapp_messages.filter(organization_id=lead.organization_id)
    inbound = (
        messages.filter(direction=WhatsAppMessage.Direction.INBOUND)
        .select_related("account")
        .order_by("-created_at", "-id")
        .first()
    )
    if inbound is None:
        return None
    human_reply = (
        messages.filter(
            account_id=inbound.account_id,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            status__in=[
                WhatsAppMessage.Status.SENT,
                WhatsAppMessage.Status.DELIVERED,
                WhatsAppMessage.Status.READ,
            ],
            created_at__gt=inbound.created_at,
        )
        .exclude(raw_payload__has_any_keys=[
            "shvya_ai", "shvya_welcome", "shvya_auto_followup", "shvya_workflow",
        ])
        .order_by("-created_at", "-id")
        .first()
    )
    return human_reply or inbound


def _has_existing_ai_response(
    *,
    lead,
    inbound_message,
    body,
):
    """
    Detect whether an AI-generated outbound WhatsApp response
    already exists for the given inbound message.

    Primary protection:
        raw_payload["shvya_ai"]["source_inbound_message_id"]

    Fallback protection:
        same outbound body created at or after the source
        inbound message timestamp.
    """
    from apps.channels.models import WhatsAppMessage

    if inbound_message is None:
        return False

    source_id = str(
        inbound_message.id
    )

    # Primary idempotency check:
    # the outbound message explicitly records which inbound
    # message caused the AI response.
    if (
        WhatsAppMessage.objects
        .filter(
            organization=lead.organization,
            lead=lead,
            direction=(
                WhatsAppMessage.Direction.OUTBOUND
            ),
            raw_payload__shvya_ai__source_inbound_message_id=(
                source_id
            ),
        )
        .exists()
    ):
        return True

    # Defensive fallback for outbound messages created before
    # the SHVYA AI metadata was attached.
    if not body:
        return False

    return (
        WhatsAppMessage.objects
        .filter(
            organization=lead.organization,
            lead=lead,
            direction=(
                WhatsAppMessage.Direction.OUTBOUND
            ),
            body=body,
            created_at__gte=inbound_message.created_at,
        )
        .exists()
    )

def _whatsapp_send_eligible(
    *,
    lead,
    inbound_message,
    account,
):
    """
    Deterministic eligibility check for an AI-generated free-form
    WhatsApp response.

    AI does not decide whether the transport is technically allowed.

    Requirements:
        - Lead must have a phone number.
        - WhatsApp account must exist.
        - Account must belong to the Lead's organization.
        - Account must be active.
        - Account must be connected.
        - Latest source message must be inbound.
        - Source inbound message must still be inside the 24-hour
          customer-service window.

    This function does not send anything.
    """

    from apps.channels.models import WhatsAppAccount

    if not (
        lead.phone
        or ""
    ).strip():
        return (
            False,
            "lead_has_no_phone_number",
        )

    if account is None:
        return (
            False,
            "no_whatsapp_account",
        )

    if (
        account.organization_id
        != lead.organization_id
    ):
        return (
            False,
            "organization_mismatch",
        )

    if not account.is_active:
        return (
            False,
            "whatsapp_account_inactive",
        )

    if (
        account.status
        != WhatsAppAccount.Status.CONNECTED
    ):
        return (
            False,
            "whatsapp_account_not_connected",
        )

    if (
        inbound_message is None
        or inbound_message.direction
        != inbound_message.Direction.INBOUND
    ):
        return (
            False,
            "source_message_not_inbound",
        )

    if (
        inbound_message.created_at
        is None
    ):
        return (
            False,
            "source_message_missing_timestamp",
        )

    expires_at = (
        inbound_message.created_at
        + timedelta(
            hours=24,
        )
    )

    if timezone.now() >= expires_at:
        return (
            False,
            "whatsapp_24h_window_expired",
        )

    return (
        True,
        "eligible",
    )

def _execute_ai_engagement_response_impl(
    *,
    task,
    lead_id: str,
):
    """
    Shared AI Engagement execution path.

    The canonical production task receives only the Lead ID and
    resolves the current Lead, organization, pipeline, stage, and
    connected WhatsApp account inside the worker.

    The execution order is:

        Lead
        -> AI permission
        -> WhatsApp account
        -> latest inbound message
        -> AI Engagement decision
        -> permission re-check
        -> conversation freshness re-check
        -> final transactional lock
        -> duplicate protection
        -> WhatsApp send eligibility
        -> CRM actions
        -> queue outbound WhatsApp message
        -> dispatch existing WhatsApp sender after commit

    This function does not call Meta directly.
    """

    from apps.ai_engagement.services.ai_permissions import (
        AIPermissionError,
        AIPermissionService,
    )
    from apps.ai_engagement.services.crm_executor import (
        CRMActionExecutionError,
        CRMActionExecutor,
    )
    from apps.ai_engagement.services.engagement import (
        EngagementError,
        EngagementService,
    )
    from apps.channels.models import (
        WhatsAppMessage,
    )
    from apps.channels.tasks import (
        send_whatsapp_message_task,
    )
    from apps.crm.models import Lead
    from services.channels.whatsapp_service import (
        queue_outbound_message,
        resolve_account_for_lead,
    )

    # --------------------------------------------------------
    # RESOLVE LEAD
    # --------------------------------------------------------

    try:
        lead = (
            Lead.objects
            .select_related(
                "organization",
                "pipeline",
                "stage",
            )
            .get(
                id=lead_id,
            )
        )

    except Lead.DoesNotExist:
        logger.warning(
            "generate_ai_engagement_response: "
            "lead %s not found",
            lead_id,
        )

        return {
            "status": "skipped",
            "reason": "lead_not_found",
            "lead_id": str(
                lead_id
            ),
        }

    organization = lead.organization

    # --------------------------------------------------------
    # INITIAL AI PERMISSION CHECK
    # --------------------------------------------------------

    try:
        permission = (
            AIPermissionService().evaluate(
                organization=organization,
                lead=lead,
            )
        )

    except AIPermissionError as exc:
        logger.error(
            "generate_ai_engagement_response: "
            "permission evaluation failed for lead %s: %s",
            lead_id,
            exc,
        )

        return {
            "status": "failed",
            "reason": (
                "ai_permission_evaluation_failed"
            ),
            "lead_id": str(
                lead_id
            ),
            "error": str(
                exc
            ),
        }

    if not permission.allowed:
        return {
            "status": "skipped",
            "reason": permission.reason,
            "lead_id": str(
                lead_id
            ),
        }

    # --------------------------------------------------------
    # RESOLVE WHATSAPP ACCOUNT
    # --------------------------------------------------------

    account = resolve_account_for_lead(
        organization=organization,
        lead=lead,
    )

    if account is None:
        return {
            "status": "skipped",
            "reason": "no_connected_whatsapp_account",
            "lead_id": str(
                lead_id
            ),
        }

    # --------------------------------------------------------
    # RESOLVE SOURCE INBOUND MESSAGE
    # --------------------------------------------------------

    latest_message = _latest_whatsapp_message(
        lead=lead,
    )

    if latest_message is None:
        return {
            "status": "skipped",
            "reason": "no_whatsapp_messages",
            "lead_id": str(
                lead_id
            ),
        }

    if (
        latest_message.direction
        != WhatsAppMessage.Direction.INBOUND
    ):
        return {
            "status": "skipped",
            "reason": (
                "latest_message_not_inbound"
            ),
            "lead_id": str(
                lead_id
            ),
            "latest_message_id": str(
                latest_message.id
            ),
        }

    source_inbound_message_id = (
        latest_message.id
    )
    # A queued/failed/sent reply must not trigger another model call simply
    # because inbound selection now ignores pending outbound transcript rows.
    # Delivery recovery owns the persisted response; generation never replaces
    # it or replays an uncertain provider outcome.
    existing_response = WhatsAppMessage.objects.filter(
        organization=organization,
        lead=lead,
        direction=WhatsAppMessage.Direction.OUTBOUND,
        raw_payload__shvya_ai__source_inbound_message_id=str(source_inbound_message_id),
    ).order_by("created_at", "id").first()
    if existing_response is not None:
        failed = existing_response.status == WhatsAppMessage.Status.FAILED
        return {
            "status": "failed" if failed else "skipped",
            "reason": "existing_ai_delivery_failed" if failed else "duplicate_ai_response",
            "lead_id": str(lead.id),
            "source_message_id": str(source_inbound_message_id),
            "message_id": str(existing_response.id),
            "delivery_status": existing_response.status,
        }

    # --------------------------------------------------------
    # AI ENGAGEMENT GENERATION
    # --------------------------------------------------------

    try:

        from apps.ai_engagement.services.turn_controller import TurnController

        decision = TurnController().engage(
            organization=organization,
            lead=lead,
        )

    except EngagementError as exc:

        provider_error = exc.__cause__

        if isinstance(
            provider_error,
            AIProviderTransientError,
        ):
            logger.warning(
                "generate_ai_engagement_response: "
                "transient provider failure for lead %s: %s",
                lead_id,
                exc,
            )

            raise task.retry(
                exc=provider_error,
                countdown=provider_retry_countdown(
                    provider_error,
                    identifier=lead_id,
                    retries=task.request.retries,
                    default=30,
                ),
            )

        logger.error(
            "generate_ai_engagement_response: "
            "permanent engagement failure for lead %s: %s",
            lead_id,
            exc,
        )

        return {
            "status": "failed",
            "reason": (
                "engagement_generation_failed"
            ),
            "lead_id": str(
                lead_id
            ),
            "error": str(
                exc
            ),
        }

    except AIProviderTransientError as exc:

        raise task.retry(
            exc=exc,
            countdown=provider_retry_countdown(
                exc,
                identifier=lead_id,
                retries=task.request.retries,
                default=30,
            ),
        )

    except Exception as exc:

        logger.exception(
            "generate_ai_engagement_response: "
            "unexpected generation failure for lead %s",
            lead_id,
        )

        raise task.retry(
            exc=exc,
        )

    # --------------------------------------------------------
    # RE-CHECK LEAD STATE AFTER AI GENERATION
    # --------------------------------------------------------

    lead.refresh_from_db(
        fields=[
            "organization",
            "pipeline",
            "stage",
            "ai_enabled",
        ],
    )

    try:

        permission = (
            AIPermissionService().evaluate(
                organization=organization,
                lead=lead,
            )
        )

    except AIPermissionError as exc:

        logger.error(
            "generate_ai_engagement_response: "
            "permission re-check failed for lead %s: %s",
            lead_id,
            exc,
        )

        return {
            "status": "failed",
            "reason": (
                "ai_permission_recheck_failed"
            ),
            "lead_id": str(
                lead_id
            ),
            "error": str(
                exc
            ),
        }

    if not permission.allowed:
        return {
            "status": "skipped",
            "reason": permission.reason,
            "lead_id": str(
                lead_id
            ),
        }

    # --------------------------------------------------------
    # RE-CHECK WHATSAPP ACCOUNT AFTER AI GENERATION
    # --------------------------------------------------------
    #
    # The account could have been disconnected while the AI
    # provider was generating the response.
    #

    account = resolve_account_for_lead(
        organization=organization,
        lead=lead,
    )

    if account is None:
        return {
            "status": "skipped",
            "reason": (
                "no_connected_whatsapp_account"
            ),
            "lead_id": str(
                lead_id
            ),
        }

    # --------------------------------------------------------
    # RE-CHECK CONVERSATION FRESHNESS
    # --------------------------------------------------------

    latest_after_generation = (
        _latest_whatsapp_message(
            lead=lead,
        )
    )

    if (
        latest_after_generation is None
        or latest_after_generation.id
        != source_inbound_message_id
        or latest_after_generation.direction
        != WhatsAppMessage.Direction.INBOUND
    ):
        return {
            "status": "skipped",
            "reason": (
                "conversation_changed_during_generation"
            ),
            "lead_id": str(
                lead_id
            ),
            "source_message_id": str(
                source_inbound_message_id
            ),
        }

    # --------------------------------------------------------
    # NO CUSTOMER-FACING ENGAGEMENT
    # --------------------------------------------------------

    if not decision.should_engage:

        try:

            with transaction.atomic():

                lead = (
                    Lead.objects
                    .select_for_update()
                    .select_related(
                        "organization",
                        "pipeline",
                        "stage",
                    )
                    .get(
                        id=lead_id,
                    )
                )

                permission = (
                    AIPermissionService().evaluate(
                        organization=organization,
                        lead=lead,
                    )
                )

                if not permission.allowed:
                    return {
                        "status": "skipped",
                        "reason": (
                            permission.reason
                        ),
                        "lead_id": str(
                            lead_id
                        ),
                    }

                latest_final = (
                    _latest_whatsapp_message(
                        lead=lead,
                    )
                )

                if (
                    latest_final is None
                    or latest_final.id
                    != source_inbound_message_id
                    or latest_final.direction
                    != WhatsAppMessage.Direction.INBOUND
                ):
                    return {
                        "status": "skipped",
                        "reason": (
                            "conversation_changed_before_finalize"
                        ),
                        "lead_id": str(
                            lead_id
                        ),
                    }

                if not _persist_engagement_answers(lead, decision, source_inbound_message_id):
                    return {"status": "skipped", "reason": "message_already_processed", "lead_id": str(lead_id)}
                crm_result = (
                    CRMActionExecutor().execute(
                        organization=organization,
                        lead=lead,
                        actions=decision.crm_actions,
                    )
                )

        except CRMActionExecutionError as exc:

            logger.error(
                "generate_ai_engagement_response: "
                "CRM action failed for lead %s: %s",
                lead_id,
                exc,
            )

            return {
                "status": "failed",
                "reason": "crm_action_failed",
                "lead_id": str(
                    lead_id
                ),
                "error": str(
                    exc
                ),
            }

        return {
            "status": "completed",
            "reason": "no_engagement",
            "lead_id": str(
                lead_id
            ),
            "crm": crm_result,
        }

    # --------------------------------------------------------
    # CUSTOMER-FACING MESSAGE VALIDATION
    # --------------------------------------------------------

    body = (
        decision.message.strip()
    )

    if not body:
        return {
            "status": "failed",
            "reason": (
                "empty_engagement_message"
            ),
            "lead_id": str(
                lead_id
            ),
        }

    # --------------------------------------------------------
    # FINAL ATOMIC CHECK + CRM + QUEUE
    # --------------------------------------------------------

    try:

        with transaction.atomic():

            lead = (
                Lead.objects
                .select_for_update()
                .select_related(
                    "organization",
                    "pipeline",
                    "stage",
                )
                .get(
                    id=lead_id,
                )
            )

            latest_final = (
                _latest_whatsapp_message(
                    lead=lead,
                )
            )

            if (
                latest_final is None
                or latest_final.id
                != source_inbound_message_id
                or latest_final.direction
                != WhatsAppMessage.Direction.INBOUND
            ):
                return {
                    "status": "skipped",
                    "reason": (
                        "conversation_changed_before_send"
                    ),
                    "lead_id": str(
                        lead_id
                    ),
                }

            permission = (
                AIPermissionService().evaluate(
                    organization=organization,
                    lead=lead,
                )
            )

            if not permission.allowed:
                return {
                    "status": "skipped",
                    "reason": permission.reason,
                    "lead_id": str(
                        lead_id
                    ),
                }

            # ------------------------------------------------
            # REFRESH WHATSAPP ACCOUNT INSIDE FINAL TRANSACTION
            # ------------------------------------------------
            #
            # The connection may have changed while AI was
            # generating the response.
            #

            account = resolve_account_for_lead(
                organization=organization,
                lead=lead,
            )

            if account is None:
                return {
                    "status": "skipped",
                    "reason": (
                        "no_connected_whatsapp_account"
                    ),
                    "lead_id": str(
                        lead_id
                    ),
                }

            # ------------------------------------------------
            # DUPLICATE AI RESPONSE PROTECTION
            # ------------------------------------------------

            if _has_existing_ai_response(
                lead=lead,
                inbound_message=latest_final,
                body=body,
            ):
                return {
                    "status": "skipped",
                    "reason": (
                        "duplicate_ai_response"
                    ),
                    "lead_id": str(
                        lead_id
                    ),
                    "source_message_id": str(
                        source_inbound_message_id
                    ),
                }

            # ------------------------------------------------
            # WHATSAPP SEND ELIGIBILITY
            # ------------------------------------------------

            send_eligible, eligibility_reason = (
                _whatsapp_send_eligible(
                    lead=lead,
                    inbound_message=latest_final,
                    account=account,
                )
            )

            if not send_eligible:
                logger.info(
                    "generate_ai_engagement_response: "
                    "WhatsApp send skipped for lead %s: %s",
                    lead_id,
                    eligibility_reason,
                )

                return {
                    "status": "skipped",
                    "reason": eligibility_reason,
                    "lead_id": str(
                        lead_id
                    ),
                    "source_message_id": str(
                        source_inbound_message_id
                    ),
                }

            # ------------------------------------------------
            # CRM ACTIONS
            # ------------------------------------------------

            if not _persist_engagement_answers(lead, decision, source_inbound_message_id):
                return {"status": "skipped", "reason": "message_already_processed", "lead_id": str(lead_id)}
            crm_result = (
                CRMActionExecutor().execute(
                    organization=organization,
                    lead=lead,
                    actions=decision.crm_actions,
                )
            )

            # ------------------------------------------------
            # QUEUE OUTBOUND WHATSAPP MESSAGE
            # ------------------------------------------------
            #
            # The EngagementDecision currently supports one explicit
            # media selection: file_document_id. When present, queue
            # the existing organization-owned Document as a WhatsApp
            # document. Otherwise preserve the existing text path.
            #
            # The actual document ownership/activity/file validation
            # is performed again by the WhatsApp send service at send
            # time, so a stale AI decision cannot send an invalid file.
            # --------------------------------------------------------

            outbound_message_kwargs = {
                "organization": organization,
                "account": account,
                "to_number": lead.phone,
                "body": body,
                "lead": lead,
            }

            if decision.file_document_id is not None:
                try:
                    document_id = int(
                        decision.file_document_id
                    )
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        "Engagement decision contains an invalid "
                        "file_document_id."
                    ) from exc

                if document_id <= 0:
                    raise ValueError(
                        "Engagement decision contains an invalid "
                        "file_document_id."
                    )

                from apps.ai_engagement.services.file_sharing import FileSharingService
                eligible_files = FileSharingService.eligible_documents(organization=organization)
                if eligible_files.exclude(share_instruction="").exists():
                    eligible_files = eligible_files.exclude(share_instruction="")
                if not eligible_files.filter(
                    id=document_id,
                ).exists():
                    raise ValueError(
                        "Engagement decision selected a file that is not "
                        "configured for AI-guided sharing."
                    )

                outbound_message_kwargs.update({
                    "message_type": (
                        WhatsAppMessage.MessageType.DOCUMENT
                    ),
                    "media_payload": {
                        "source": "document",
                        "document_id": document_id,
                    },
                })

            outbound_message = (
                queue_outbound_message(
                    **outbound_message_kwargs
                )
            )

            # ------------------------------------------------
            # STORE AI METADATA FOR IDEMPOTENCY / AUDIT
            # ------------------------------------------------

            outbound_message.raw_payload = {
                "shvya_ai": {
                    "source_inbound_message_id": (
                        str(
                            source_inbound_message_id
                        )
                    ),
                    # Preserve arrival order even when concurrent generations
                    # finish in a different order across leads.
                    "queued_at": latest_final.created_at.isoformat(),
                    "model": decision.model,
                    "reason": decision.reason,
                    "next_requirement_id": decision.next_requirement_id,
                }
            }

            outbound_message.save(
                update_fields=[
                    "raw_payload",
                    "updated_at",
                ],
            )

            # ------------------------------------------------
            # DISPATCH EXISTING WHATSAPP SENDER AFTER COMMIT
            # ------------------------------------------------

            transaction.on_commit(
                lambda message_id=outbound_message.id: (
                    send_whatsapp_message_task.delay(
                        str(
                            message_id
                        )
                    )
                )
            )

    except (
        CRMActionExecutionError,
        AIPermissionError,
    ) as exc:

        logger.error(
            "generate_ai_engagement_response: "
            "finalization failed for lead %s: %s",
            lead_id,
            exc,
        )

        return {
            "status": "failed",
            "reason": "finalization_failed",
            "lead_id": str(
                lead_id
            ),
            "error": str(
                exc
            ),
        }

    except Exception as exc:

        logger.exception(
            "generate_ai_engagement_response: "
            "unexpected finalization failure for lead %s",
            lead_id,
        )

        raise task.retry(
            exc=exc,
        )

    return {
        "status": "completed",
        "lead_id": str(
            lead_id
        ),
        "engaged": True,
        "crm": crm_result,
        "message_id": str(
            outbound_message.id
        ),
        "source_message_id": str(
            source_inbound_message_id
        ),
        "model": decision.model,
    }
