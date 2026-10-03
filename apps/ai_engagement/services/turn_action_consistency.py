"""Read source-bound action evidence; never execute or authorize mutations."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from uuid import UUID


ACTION_TYPES = frozenset({
    "attribute_updates", "pipeline_transition", "create_reminder", "contact_updates", "add_note",
})
RESULT_STATUSES = frozenset({"executed", "no_op", "not_applied", "partial", "failed"})


def policy_fingerprint(organization_context):
    data = organization_context if isinstance(organization_context, dict) else {}
    # Hash the authored specification, not model-rewritten/compacted instructions.
    policy = {key: data.get(key) for key in ("ai_playbook", "bot_languages", "ai_enabled")}
    return hashlib.sha256(json.dumps(policy, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def assert_policy_current(*, organization, decision):
    """Reject an in-flight generated action when its authored policy changed.

    Legacy deterministic/admin callers do not carry a generated policy revision;
    they remain subject to the executor's existing current-policy authorization.
    """
    expected = getattr(decision, "policy_revision", "")
    if not expected:
        return
    from apps.ai_engagement.models import OrgInfo
    from apps.ai_engagement.services.crm_executor import CRMActionExecutionError

    from django.db import connection
    query = OrgInfo.objects.filter(organization=organization)
    if connection.in_atomic_block:
        query = query.select_for_update()
    current = query.values(
        "ai_playbook", "bot_languages", "ai_enabled",
    ).first()
    if current is None or policy_fingerprint(current) != expected:
        raise CRMActionExecutionError("AI Playbook changed during this turn; regenerate before executing actions.")


def applied_action_types(results):
    return list(dict.fromkeys(
        result["type"] for result in results or []
        if isinstance(result, dict) and isinstance(result.get("type"), str) and result.get("type") in ACTION_TYPES
        and isinstance(result.get("status"), str) and result.get("status") in {"executed", "partial"}
        and (result.get("type") != "attribute_updates" or bool(result.get("keys")))
    ))


def safe_action_results(results):
    """Outcome codes/counts only: do not duplicate notes, handles or field values."""
    output = []
    for result in (results or [])[:40]:
        if not isinstance(result, dict) or not isinstance(result.get("type"), str) or result.get("type") not in ACTION_TYPES:
            continue
        status = result.get("status")
        if not isinstance(status, str) or status not in RESULT_STATUSES:
            status = "not_applied"
        if result["type"] == "attribute_updates" and status == "executed" and not result.get("keys"):
            status = "not_applied"
        row = {"type": result["type"], "status": status,
               "idempotent_replay": result.get("idempotent_replay") is True}
        for key in ("keys", "created_keys", "skipped_keys"):
            values = result.get(key)
            if isinstance(values, list):
                row[key] = [value[:100] for value in values[:30] if isinstance(value, str)]
        # IDs are internal verification references, never customer-facing text.
        for key in ("reminder_id", "stage_id", "pipeline_id", "note_id"):
            value = result.get(key)
            try:
                row[key] = str(UUID(str(value)))
            except (ValueError, TypeError, AttributeError):
                continue
        output.append(row)
    return output


def receipts_for_source(*, lead, source):
    """The caller supplies a tenant/lead-validated persisted inbound record."""
    from apps.ai_engagement.models import AIActionReceipt
    from apps.ai_engagement.services.tenant_guard import TenantGuard

    if source is None or getattr(source, "direction", None) != "inbound":
        return []
    TenantGuard(lead.organization).validate_message(source, lead=lead)
    results = list(AIActionReceipt.objects.filter(
        organization_id=lead.organization_id, lead_id=lead.pk, source_message_id=source.pk,
    ).order_by("created_at", "id").values_list("result", flat=True)[:40])
    return safe_action_results(results)


def source_for_context(*, lead, context):
    conversation = getattr(context, "conversation", None) or {}
    source_id = next((str(item.get("id") or "") for item in reversed(conversation.get("messages") or [])
                      if isinstance(item, dict) and item.get("direction") == "inbound"), "")
    try:
        UUID(source_id)
    except (ValueError, TypeError, AttributeError):
        return None
    if conversation.get("channel") == "instagram":
        from apps.channels.instagram_models import InstagramMessage
        query = InstagramMessage.objects.select_related("account", "conversation", "conversation__lead").filter(
            pk=source_id, organization_id=lead.organization_id, conversation__lead=lead,
            conversation__organization_id=lead.organization_id, account__organization_id=lead.organization_id,
            direction="inbound",
        )
        if conversation.get("conversation_id"):
            query = query.filter(conversation_id=conversation["conversation_id"])
        return query.first()
    from apps.channels.models import WhatsAppMessage
    return WhatsAppMessage.objects.select_related("account", "lead").filter(
        pk=source_id, organization_id=lead.organization_id, lead=lead,
        account__organization_id=lead.organization_id, direction="inbound",
    ).first()


def live_operational_state(*, lead, context):
    from apps.ai_engagement.services.file_delivery_receipts import source_file_state
    from apps.ai_engagement.services.runtime_state import STATE_KEY
    from apps.crm.models import LeadReminder

    source = source_for_context(lead=lead, context=context)
    outcomes = receipts_for_source(lead=lead, source=source)
    types = applied_action_types(outcomes)
    attrs = lead.attributes if isinstance(lead.attributes, dict) else {}
    runtime = attrs.get(STATE_KEY) if isinstance(attrs.get(STATE_KEY), dict) else {}
    raw = source.raw_payload if source and isinstance(source.raw_payload, dict) else {}
    processing = raw.get("shvya_ai_processing") or {}
    if not isinstance(processing, dict):
        processing = {}
    resolved = {"source_message_id": str(source.pk) if source else None,
                "action_types": types, "outcomes": outcomes,
                "authority": "persisted_source_action_receipts"}
    if source is not None:
        if getattr(source, "conversation_id", None) is not None:
            from apps.ai_engagement.services.instagram_delivery_outcomes import instagram_file_state
            file_state = instagram_file_state(lead=lead, source=source)
        else:
            file_state = source_file_state(lead=lead, source=source, processing=processing, runtime=runtime)
        if file_state.get("document_id") is not None:
            resolved["file_share"] = file_state
            # Eligibility/queueing is not a completed send.
            if file_state.get("status") in {"sent", "read", "delivered"}:
                resolved["action_types"] = [*types, "file_share"]
    reminders = list(LeadReminder.objects.filter(
        lead=lead, lead__organization_id=lead.organization_id, status="pending",
    ).order_by("due_at", "created_at").values("id", "title", "description", "due_at", "status")[:10])
    for reminder in reminders:
        reminder["id"] = str(reminder["id"])
        reminder["due_at"] = reminder["due_at"].isoformat() if reminder["due_at"] else None
    # A previous pending reminder can inform the reply; it does not authorize
    # claiming a newly requested callback was scheduled in the current turn.
    return {"pending_reminders": reminders, "resolved_actions": deepcopy(resolved)}
