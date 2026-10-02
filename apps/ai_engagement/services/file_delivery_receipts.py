"""Source-bound file outcomes over the existing message and CRM records.

No sending, retries, new tables or action authorization live here. Transport
records remain authoritative; projections must not turn queueing into delivery.
"""
from __future__ import annotations

from copy import deepcopy
from uuid import UUID


FILE_ID_KEY = "pre_resolved_file_document_id"
FILE_STATUS_KEY = "pre_resolved_file_status"
SOURCE_KEY = "pre_resolved_message_id"
SUCCESS = frozenset({"sent", "delivered", "read"})
TERMINAL = SUCCESS | {"failed"}


def positive_id(value):
    """Reject bools, floats, containers and out-of-range database identifiers."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    text = str(value)
    if not text.isascii() or not text.isdigit() or len(text) > 19:
        return None
    number = int(text)
    return number if 0 < number <= 9223372036854775807 else None


def valid_uuid(value):
    try:
        UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return False
    return True


def source_file_reference(*, processing, runtime, source_id):
    """An empty current-turn selection never inherits an older turn's file."""
    processing = processing if isinstance(processing, dict) else {}
    runtime = runtime if isinstance(runtime, dict) else {}
    if "resolved_file_document_id" in processing:
        return (positive_id(processing.get("resolved_file_document_id")),
                str(processing.get("file_share_status") or "none"))
    if source_id and str(runtime.get(SOURCE_KEY) or "") == str(source_id):
        return positive_id(runtime.get(FILE_ID_KEY)), str(runtime.get(FILE_STATUS_KEY) or "none")
    return None, "none"


def advance_status(previous, persisted):
    """Preserve delivered/read acknowledgements for the SAME outbound message.

    `sent` is provider acceptance, not confirmed delivery. A persisted failure
    may replace `sent`; a later successful retry may replace `failed`. Callers
    must supply freshly read transport state, never a model's proposed status.
    """
    if previous == "read":
        return "read"
    if previous == "delivered" and persisted != "read":
        return "delivered"
    return persisted if isinstance(persisted, str) and persisted in TERMINAL else None


def shared_history(history, *, document_id, source_id, message_id, status, now):
    """Track successful outbound attempts without corrupt legacy IDs crashing."""
    history = history if isinstance(history, list) else []
    clean = []
    previous = None
    for item in history[-30:]:
        if not isinstance(item, dict) or positive_id(item.get("document_id")) is None:
            continue
        if str(item.get("message_id") or "") == message_id:
            previous = item
        else:
            clean.append(deepcopy(item))
    if status in SUCCESS:
        clean.append({
            "document_id": document_id, "source_message_id": source_id,
            "message_id": message_id, "status": status,
            "sent_at": (previous or {}).get("sent_at") or now,
        })
    return clean[-30:]


def _outbound_matches(outbound, *, source_id, document_id):
    payload = outbound.raw_payload if isinstance(outbound.raw_payload, dict) else {}
    meta = payload.get("shvya_ai")
    meta = meta if isinstance(meta, dict) else {}
    media = outbound.media_payload if isinstance(outbound.media_payload, dict) else {}
    return (str(meta.get("source_inbound_message_id") or "") == str(source_id)
            and media.get("source") == "document"
            and positive_id(media.get("document_id")) == document_id)


def source_file_state(*, lead, source, processing, runtime):
    """Read current transport status; never rely solely on an old snapshot."""
    from apps.ai_engagement.models import Document
    from apps.channels.models import WhatsAppMessage

    empty = {"document_id": None, "document_name": "", "status": "none"}
    if (source is None or str(source.lead_id) != str(lead.pk)
            or str(source.organization_id) != str(lead.organization_id)
            or source.direction != WhatsAppMessage.Direction.INBOUND):
        return empty
    document_id, status = source_file_reference(
        processing=processing, runtime=runtime, source_id=str(source.pk),
    )
    if document_id is None:
        return empty
    document = Document.objects.filter(
        pk=document_id, organization_id=lead.organization_id,
    ).only("name").first()
    if document is None:
        return {**empty, "status": "unavailable"}
    result = {"document_id": document_id, "document_name": document.name, "status": status}
    processing = processing if isinstance(processing, dict) else {}
    message_id = processing.get("file_share_message_id")
    if not valid_uuid(message_id):
        # A selected file with no transport record has not been sent.
        if status in TERMINAL:
            result["status"] = "delivery_unknown"
        return result
    outbound = WhatsAppMessage.objects.filter(
        pk=message_id, organization_id=lead.organization_id, lead_id=lead.pk,
        direction=WhatsAppMessage.Direction.OUTBOUND,
        account__organization_id=lead.organization_id,
    ).only("raw_payload", "media_payload", "status").first()
    if outbound is None or not _outbound_matches(
        outbound, source_id=source.pk, document_id=document_id,
    ):
        result["status"] = "delivery_unknown"
        return result
    result["status"] = advance_status(status, outbound.status) or (
        "queued" if outbound.status == WhatsAppMessage.Status.QUEUED else "delivery_unknown"
    )
    return result


def record_file_delivery(*, message_id, status):
    """Update existing projections under the lead lock; never schedule a send."""
    from django.db import transaction
    from django.utils import timezone
    from apps.ai_engagement.models import Document
    from apps.ai_engagement.services.runtime_state import STATE_KEY
    from apps.channels.models import WhatsAppMessage
    from apps.crm.models import Lead

    if not valid_uuid(message_id) or not isinstance(status, str) or status not in TERMINAL:
        return False
    identity = WhatsAppMessage.objects.filter(
        pk=message_id, direction=WhatsAppMessage.Direction.OUTBOUND,
    ).values("lead_id", "organization_id").first()
    if not identity or not identity["lead_id"]:
        return False
    with transaction.atomic():
        lead = Lead.objects.select_for_update().filter(
            pk=identity["lead_id"], organization_id=identity["organization_id"],
        ).first()
        if lead is None:
            return False
        outbound = WhatsAppMessage.objects.filter(
            pk=message_id, lead=lead, organization_id=lead.organization_id,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            account__organization_id=lead.organization_id,
        ).first()
        if outbound is None or outbound.status not in TERMINAL:
            return False
        payload = outbound.raw_payload if isinstance(outbound.raw_payload, dict) else {}
        meta = payload.get("shvya_ai")
        meta = meta if isinstance(meta, dict) else {}
        source_id = str(meta.get("source_inbound_message_id") or "")
        media = outbound.media_payload if isinstance(outbound.media_payload, dict) else {}
        document_id = positive_id(media.get("document_id"))
        if media.get("source") != "document" or not document_id or not valid_uuid(source_id):
            return False
        if not Document.objects.filter(pk=document_id, organization_id=lead.organization_id).exists():
            return False
        inbound = WhatsAppMessage.objects.select_for_update().filter(
            pk=source_id, lead=lead, organization_id=lead.organization_id,
            direction=WhatsAppMessage.Direction.INBOUND,
        ).first()
        if inbound is None:
            return False
        payload = deepcopy(inbound.raw_payload) if isinstance(inbound.raw_payload, dict) else {}
        processing = payload.get("shvya_ai_processing")
        processing = deepcopy(processing) if isinstance(processing, dict) else {}
        bound_id = positive_id(processing.get("resolved_file_document_id"))
        if "resolved_file_document_id" in processing and bound_id != document_id:
            return False
        prior_message = str(processing.get("file_share_message_id") or "")
        replaced_attempt = ""
        if prior_message and prior_message != str(outbound.pk):
            if not valid_uuid(prior_message):
                return False
            prior = WhatsAppMessage.objects.filter(
                pk=prior_message, lead=lead, organization_id=lead.organization_id,
                direction=WhatsAppMessage.Direction.OUTBOUND,
            ).only("status", "created_at", "raw_payload", "media_payload").first()
            # Only an actually failed, older attempt can be superseded. A late
            # callback must not replace a newer, queued or confirmed attempt.
            if (prior is None or prior.status != "failed"
                    or processing.get("file_share_status") in {"delivered", "read"}
                    or outbound.created_at <= prior.created_at
                    or not _outbound_matches(prior, source_id=source_id, document_id=document_id)):
                return False
            replaced_attempt = prior_message
        previous = (processing.get("file_share_status")
                    if prior_message == str(outbound.pk) else None)
        resolved = advance_status(previous, outbound.status)
        now = timezone.now().isoformat()
        processing.update(resolved_file_document_id=document_id, file_share_status=resolved,
                          file_share_message_id=str(outbound.pk))
        if previous != resolved or not prior_message:
            processing["file_share_updated_at"] = now
        # Raw provider errors stay in the canonical message, not in model context.
        if resolved == "failed":
            processing["file_share_reason"] = "file_delivery_failed"
        else:
            processing.pop("file_share_reason", None)
        reconciled = processing.get("reconciled_state")
        if isinstance(reconciled, dict) and str(reconciled.get("source_message_id") or "") == source_id:
            reconciled = deepcopy(reconciled)
            file_state = reconciled.get("file_share")
            if isinstance(file_state, dict) and positive_id(file_state.get("document_id")) == document_id:
                reconciled["file_share"] = {**file_state, "status": resolved}
                processing["reconciled_state"] = reconciled
        payload["shvya_ai_processing"] = processing
        if payload != inbound.raw_payload:
            inbound.raw_payload = payload
            inbound.save(update_fields=["raw_payload", "updated_at"])
        attributes = deepcopy(lead.attributes) if isinstance(lead.attributes, dict) else {}
        runtime = attributes.get(STATE_KEY)
        runtime = deepcopy(runtime) if isinstance(runtime, dict) else {}
        # A delayed result for turn A may update A's history, never turn B's selection.
        if (str(runtime.get(SOURCE_KEY) or "") == source_id
                and positive_id(runtime.get(FILE_ID_KEY)) == document_id):
            runtime[FILE_STATUS_KEY] = resolved
        history = runtime.get("shared_files")
        if replaced_attempt and isinstance(history, list):
            history = [item for item in history if not isinstance(item, dict)
                       or str(item.get("message_id") or "") != replaced_attempt]
        runtime["shared_files"] = shared_history(
            history, document_id=document_id, source_id=source_id,
            message_id=str(outbound.pk), status=resolved, now=now,
        )
        attributes[STATE_KEY] = runtime
        if attributes != lead.attributes:
            Lead.objects.filter(pk=lead.pk, organization_id=lead.organization_id).update(attributes=attributes)
        return True
