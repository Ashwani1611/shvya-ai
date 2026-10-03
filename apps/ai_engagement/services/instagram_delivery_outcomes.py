"""Source-bound Instagram outcomes over its canonical transport rows.

No sender/retry policy lives here. READ is a real Meta acknowledgement; SENT is
only provider acceptance. Instagram does not expose a DELIVERED state here.
"""
from __future__ import annotations

import logging
from copy import deepcopy

from apps.ai_engagement.services.file_delivery_receipts import positive_id, valid_uuid


logger = logging.getLogger(__name__)


def transport_status(message):
    raw = message.raw_payload if isinstance(message.raw_payload, dict) else {}
    status = str(message.status or "")
    if status in {"sent", "read"}:
        return status if message.external_id else "delivery_unknown"
    if status == "failed":
        return "delivery_unknown" if raw.get("shvya_send_outcome") == "delivery_unknown" else "failed"
    if status == "queued":
        return "delivery_unknown" if raw.get("shvya_send_started_at") else "queued"
    return "delivery_unknown"


def instagram_file_state(*, lead, source):
    from apps.ai_engagement.models import Document
    from apps.channels.instagram_models import InstagramMessage
    from apps.ai_engagement.services.tenant_guard import TenantGuard

    empty = {"document_id": None, "document_name": "", "status": "none"}
    if source is None or source.direction != "inbound" or not getattr(source, "conversation_id", None):
        return empty
    TenantGuard(lead.organization).validate_message(source, lead=lead)
    raw = source.raw_payload if isinstance(source.raw_payload, dict) else {}
    processing = raw.get("shvya_ai_processing")
    processing = processing if isinstance(processing, dict) else {}
    document_id = positive_id(processing.get("resolved_file_document_id"))
    if document_id is None:
        return empty
    document = Document.objects.filter(pk=document_id, organization_id=lead.organization_id).only("name", "version").first()
    if document is None:
        return {**empty, "status": "unavailable"}
    result = {"document_id": document_id, "document_name": document.name,
              "status": "resolved_pending_send", "channel": "instagram"}
    messages = list(InstagramMessage.objects.filter(
        organization_id=lead.organization_id, account_id=source.account_id,
        account__organization_id=lead.organization_id, conversation_id=source.conversation_id,
        conversation__lead=lead, direction="outbound",
        raw_payload__shvya_ai__source_inbound_message_id=str(source.pk),
        raw_payload__shvya_ai__file_document_id=document_id,
        raw_payload__shvya_ai__file_version=document.version,
    ).order_by("-created_at", "-id")[:20])
    # A valid successful earlier attempt is still evidence that this file was
    # submitted; a delayed failure from another attempt cannot erase it.
    selected = next((item for item in messages if transport_status(item) == "read"), None)
    selected = selected or next((item for item in messages if transport_status(item) == "sent"), None)
    selected = selected or (messages[0] if messages else None)
    if selected is not None:
        result.update(status=transport_status(selected), message_id=str(selected.pk))
    elif processing.get("file_share_status") in {"sent", "read", "delivered", "failed", "delivery_unknown"}:
        result["status"] = "delivery_unknown"
    return result


def record_instagram_delivery(message_id):
    """Reconcile one source after commit; row-derived identity and bounded history."""
    from django.db import transaction
    from django.utils import timezone
    from apps.ai_engagement.models import Document
    from apps.channels.instagram_models import InstagramConversation, InstagramMessage
    from apps.crm.models import Lead

    if not valid_uuid(message_id):
        return False
    identity = InstagramMessage.objects.filter(pk=message_id, direction="outbound").values(
        "organization_id", "conversation_id", "account_id",
    ).first()
    if not identity:
        return False
    with transaction.atomic():
        # Match the executor's conversation -> lead -> source lock order.
        conversation = InstagramConversation.objects.select_for_update().filter(
            pk=identity["conversation_id"], organization_id=identity["organization_id"],
            account_id=identity["account_id"], account__organization_id=identity["organization_id"],
        ).first()
        if conversation is None or not conversation.lead_id:
            return False
        lead = Lead.objects.select_for_update().filter(
            pk=conversation.lead_id, organization_id=identity["organization_id"],
        ).first()
        if lead is None:
            return False
        outbound = InstagramMessage.objects.filter(
            pk=message_id, organization_id=lead.organization_id, conversation=conversation,
            account_id=conversation.account_id, direction="outbound",
        ).first()
        if outbound is None:
            return False
        raw = outbound.raw_payload if isinstance(outbound.raw_payload, dict) else {}
        metadata = raw.get("shvya_ai")
        metadata = metadata if isinstance(metadata, dict) else {}
        source_id = metadata.get("source_inbound_message_id")
        document_id = positive_id(metadata.get("file_document_id"))
        if not valid_uuid(source_id) or not document_id:
            return False
        version = positive_id(metadata.get("file_version"))
        if not version or not Document.objects.filter(pk=document_id, organization_id=lead.organization_id,
                                                      version=version).exists():
            return False
        source = InstagramMessage.objects.select_for_update().select_related(
            "account", "conversation", "conversation__lead",
        ).filter(pk=source_id, organization_id=lead.organization_id, conversation=conversation,
                 account_id=conversation.account_id, direction="inbound").first()
        if source is None:
            return False
        payload = deepcopy(source.raw_payload) if isinstance(source.raw_payload, dict) else {}
        processing = payload.get("shvya_ai_processing")
        processing = deepcopy(processing) if isinstance(processing, dict) else {}
        if positive_id(processing.get("resolved_file_document_id")) != document_id:
            return False
        status = transport_status(outbound)
        history = processing.get("file_delivery_outcomes")
        history = [item for item in (history if isinstance(history, list) else [])[-20:] if isinstance(item, dict)]
        old = next((item for item in history if item.get("message_id") == str(outbound.pk)), {})
        if old.get("status") == "read":
            status = "read"
        receipt = {"message_id": str(outbound.pk), "document_id": document_id,
                   "status": status, "recorded_at": old.get("recorded_at") or timezone.now().isoformat()}
        history = [item for item in history if item.get("message_id") != str(outbound.pk)]
        history.append(receipt)
        processing["file_delivery_outcomes"] = history[-20:]
        state = instagram_file_state(lead=lead, source=source)
        processing["file_share_status"] = state["status"]
        processing["file_share_message_id"] = state.get("message_id")
        # Never write WhatsApp's lead-level shared-files history. This source and
        # exact Instagram conversation are the only projection updated here.
        if processing == payload.get("shvya_ai_processing"):
            return False
        payload["shvya_ai_processing"] = processing
        source.raw_payload = payload
        source.save(update_fields=["raw_payload", "updated_at"])
    return True


def safely_record_instagram_delivery(message_id):
    try:
        return record_instagram_delivery(message_id)
    except Exception:
        # A projection outage must never cause an already submitted send to retry.
        logger.warning("Instagram outcome projection unavailable for message %s", message_id)
        return False
