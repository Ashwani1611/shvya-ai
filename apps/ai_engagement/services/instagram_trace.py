"""Observe exact Instagram turns without changing their execution authority."""
from functools import wraps
import logging

from apps.ai_engagement.services.turn_scope import isolated_ai_turn

logger = logging.getLogger(__name__)


def traced_instagram_turn(method):
    @wraps(method)
    @isolated_ai_turn
    def wrapped(*, task, message_id):
        from apps.ai_engagement.services import trace_service as traces
        from apps.channels.instagram_models import InstagramMessage

        source = InstagramMessage.objects.select_related(
            "organization", "account", "conversation__lead__pipeline", "conversation__lead__stage",
        ).filter(pk=message_id, direction="inbound").first()
        if source is None:
            return method(task=task, message_id=message_id)
        lead = source.conversation.lead
        from apps.ai_engagement.services.tenant_guard import TenantGuard
        guard = TenantGuard(source.organization)
        guard.validate_message(source, lead=lead, account=source.account)
        if lead is not None:
            guard.validate_current_lead_context(lead)
        token = traces.begin_trace(
            organization=source.organization, lead=lead, source_message=source, account=source.account,
        )
        traces.record("identity", {
            "connection_type": "instagram", "instagram_account_id": str(source.account_id),
            "conversation_id": str(source.conversation_id), "source_inbound_message_id": str(source.pk),
        })
        try:
            result = method(task=task, message_id=message_id)
            traces.finalize_from_result(result)
            if result.get("reason") in {"existing_ai_response_requeued", "source_already_processed"}:
                traces.record("finalization", {"existing_response_reused": True, "decision_accepted": False})
                traces.current().data["status"] = "duplicate"
            return result
        except Exception as exc:
            traces.mark_error(step="instagram_runtime", exc=exc, code="INSTAGRAM_RUNTIME_EXCEPTION")
            buffer = traces.current()
            if buffer is not None:
                buffer.data["status"] = "failed"
                buffer.data["reason_code"] = "INSTAGRAM_RUNTIME_EXCEPTION"
            raise
        finally:
            # The normal executor may have created the lead. Attach this same
            # trace buffer afterward; observability must never create a lead.
            buffer = traces.current()
            if buffer is not None and buffer.trace_id is None:
                try:
                    source.conversation.refresh_from_db()
                    lead = source.conversation.lead
                    if lead is not None:
                        inner = traces.begin_trace(organization=source.organization, lead=lead,
                                                   source_message=source, account=source.account)
                        try:
                            buffer.trace_id = traces.current().trace_id
                        finally:
                            traces._CURRENT.reset(inner)
                        # Sends may already have been observed before this
                        # newly created lead supplied a valid trace identity.
                        outbound = InstagramMessage.objects.filter(
                            organization=source.organization, account=source.account,
                            conversation=source.conversation, direction="outbound",
                            raw_payload__shvya_ai__source_inbound_message_id=str(source.pk),
                        ).order_by("-updated_at", "-id").first()
                        if outbound is not None:
                            traces.safe_delivery_update(organization_id=source.organization_id,
                                                        source_message_id=source.pk, outbound_message=outbound)
                except Exception:
                    logger.exception("Instagram trace attachment failed; AI result remains unchanged")
            traces.flush(reset_token=token)
    return wrapped
