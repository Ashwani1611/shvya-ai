"""Read Instagram's own source-bound transport evidence; never infer delivery.

No sender, webhook, retry queue or WhatsApp state is added here. SENT records
provider acceptance, READ records a persisted read receipt, and a claimed send
without a provider ID is uncertain rather than safe to repeat.
"""
from __future__ import annotations


def transport_outcome(*, status, external_id, claimed=False):
    if status == "read" and external_id:
        return "read"
    if status == "sent" and external_id:
        return "sent"
    if external_id or status in {"sent", "read"} or claimed:
        return "outcome_unknown"
    return status if status in {"queued", "failed"} else "outcome_unknown"


def file_outcome(*, source, lead):
    from apps.ai_engagement.models import Document
    from apps.ai_engagement.services.file_delivery_receipts import positive_id
    from apps.channels.instagram_models import InstagramMessage

    if (source.organization_id != lead.organization_id
            or source.direction != InstagramMessage.Direction.INBOUND
            or source.conversation.lead_id != lead.pk
            or source.conversation.organization_id != lead.organization_id
            or source.account.organization_id != lead.organization_id
            or source.account_id != source.conversation.account_id):
        return None
    payload = source.raw_payload if isinstance(source.raw_payload, dict) else {}
    processing = payload.get("shvya_ai_processing")
    processing = processing if isinstance(processing, dict) else {}
    document_id = positive_id(processing.get("resolved_file_document_id"))
    if document_id is None:
        return None
    document = Document.objects.filter(pk=document_id, organization_id=lead.organization_id).first()
    if document is None:
        return None
    row = InstagramMessage.objects.filter(
        organization_id=lead.organization_id, account_id=source.account_id,
        conversation_id=source.conversation_id, conversation__lead=lead,
        direction=InstagramMessage.Direction.OUTBOUND,
        raw_payload__shvya_ai__source_inbound_message_id=str(source.pk),
        raw_payload__shvya_ai__file_document_id=document_id,
    ).order_by("-created_at", "-pk").first()
    outcome = {"document_id": document_id,
        "document_name": document.name if document else "configured file",
        "status": "resolved_pending_send", "channel": "instagram",
        "provider_accepted": False, "recipient_read_confirmed": False,
        "delivery_confirmation_available": False}
    if row is None:
        return outcome
    raw = row.raw_payload if isinstance(row.raw_payload, dict) else {}
    status = transport_outcome(status=row.status, external_id=row.external_id,
                               claimed=bool(raw.get("shvya_send_claimed_at")))
    return {**outcome, "status": status, "outbound_message_id": str(row.pk),
        "provider_accepted": bool(row.external_id and status in {"sent", "read"}),
        "recipient_read_confirmed": status == "read",
        "sent_at": row.sent_at.isoformat() if row.sent_at else None}
