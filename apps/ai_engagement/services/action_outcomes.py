"""Source-bound, read-only action evidence for generation and final grounding.

AIActionReceipt is authoritative for what committed, not model proposals or a
lead-wide marker. Safe current-state checks qualify historical receipts without
putting private note bodies or contact handles into the generation payload.
"""
from __future__ import annotations

import hashlib
import json
from uuid import UUID


MAX_OUTCOMES = 50
ACTION_NAMES = {
    "UPDATE_ATTRIBUTE": "attribute_updates", "MOVE_STAGE": "pipeline_transition",
    "CREATE_REMINDER": "create_reminder", "ADD_NOTE": "add_note", "UPDATE_CONTACT": "contact_updates",
}


def action_policy_revision(profile) -> str:
    """Ignore knowledge/attribute additions; bind authored rules and capabilities."""
    data = profile.as_dict()
    crm = data.get("crm_capabilities") or {}
    payload = {"organization_id": data.get("organization_id"),
               "ai_instructions": data.get("ai_instructions"),
               "qualification": data.get("qualification"),
               "allowed_action_types": crm.get("allowed_action_types"),
               "pipelines": crm.get("pipelines")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _uuid(value):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def source_for_context(*, context, lead):
    conversation = context.conversation if isinstance(context.conversation, dict) else {}
    source_id = next((item.get("id") for item in reversed(conversation.get("messages") or [])
                      if isinstance(item, dict) and item.get("direction") == "inbound"), None)
    if _uuid(source_id) is None:
        return None
    if conversation.get("channel") == "instagram":
        from apps.channels.instagram_models import InstagramMessage
        query = InstagramMessage.objects.select_related("account", "conversation").filter(
            pk=source_id, organization_id=lead.organization_id, conversation__lead=lead, direction="inbound")
        if conversation.get("id"):
            if _uuid(conversation["id"]) is None:
                return None
            query = query.filter(conversation_id=conversation["id"])
        source = query.first()
        if source is not None and (source.conversation.organization_id != lead.organization_id
                or source.account_id != source.conversation.account_id):
            return None
    else:
        from apps.channels.models import WhatsAppMessage
        source = WhatsAppMessage.objects.select_related("account").filter(
            pk=source_id, organization_id=lead.organization_id, lead=lead, direction="inbound").first()
    if source is not None and source.account.organization_id != lead.organization_id:
        return None
    return source


def action_outcomes(*, lead, source) -> dict:
    from apps.ai_engagement.models import AIActionReceipt
    from apps.ai_engagement.services.confidentiality import is_sensitive_field_name
    from apps.crm.models import LeadReminder, LeadNote, LeadContact, Stage

    if source is None or source.organization_id != lead.organization_id:
        return {"authority": "source_unavailable", "outcomes": [], "truncated": False}
    receipts = list(AIActionReceipt.objects.filter(organization_id=lead.organization_id, lead=lead,
        source_message_id=source.pk).order_by("created_at", "id")[:MAX_OUTCOMES + 1])
    outcomes = []
    for receipt in receipts[:MAX_OUTCOMES]:
        result = receipt.result if isinstance(receipt.result, dict) else {}
        kind = ACTION_NAMES.get(receipt.action_type)
        if kind is None or result.get("type") != kind:
            continue
        status = result.get("status") if result.get("status") in {"executed", "no_op", "failed"} else "unknown"
        row = {"type": kind, "status": status, "recorded_at": receipt.created_at.isoformat()}
        if kind == "attribute_updates":
            keys = result.get("keys")
            keys = keys if isinstance(keys, list) else []
            row["keys"] = [key for key in keys[:30]
                           if isinstance(key, str) and not key.startswith("_") and not is_sensitive_field_name(key)]
            row["verification"] = "committed_receipt_not_current_value_assertion"
        elif kind == "pipeline_transition":
            target = Stage.objects.filter(pk=_uuid(result.get("stage_id")),
                pipeline__organization_id=lead.organization_id).first()
            row["current_state_matches"] = bool(target and target.pk == lead.stage_id)
        elif kind == "create_reminder":
            reminder = LeadReminder.objects.filter(pk=_uuid(result.get("reminder_id")),
                lead=lead, lead__organization_id=lead.organization_id).first()
            row["still_exists"] = reminder is not None
            row["current_status"] = reminder.status if reminder else "absent"
            if reminder:
                row["due_at"] = reminder.due_at.isoformat()
        elif kind == "add_note":
            row["still_exists"] = LeadNote.objects.filter(pk=_uuid(result.get("note_id")), lead=lead,
                lead__organization_id=lead.organization_id).exists()
        elif kind == "contact_updates":
            values = result.get("contact_ids")
            values = values if isinstance(values, list) else []
            ids = [_uuid(value) for value in values[:30]]
            row["present_contact_count"] = LeadContact.objects.filter(pk__in=[value for value in ids if value],
                lead=lead, lead__organization_id=lead.organization_id).count()
        outcomes.append(row)
    return {"authority": "source_bound_committed_receipts", "outcomes": outcomes,
            "truncated": len(receipts) > MAX_OUTCOMES}
