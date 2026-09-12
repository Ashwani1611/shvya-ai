"""Install deterministic WhatsApp AI orchestration behavior.

This keeps channel transport code stable while replacing legacy eager summary
work, generic positive-reply stage movement, and long Hosted AI delay with the
precise SHVYA orchestration rules.
"""

from __future__ import annotations

import os
import sys

from services.channels.reply_intent_service import Intent, classify_reply


_INSTALLED = False


def _debounce_seconds() -> int:
    try:
        value = int(os.getenv("AI_ENGAGEMENT_DEBOUNCE_SECONDS", "5"))
    except (TypeError, ValueError):
        value = 5
    # Leave time for generation and delivery inside the hosted 30-second target.
    return min(max(value, 0), 5)


def _normalized_text(value: str) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _conversation_bound_hosted_block_reason(*, account, lead):
    """Apply live AI controls without re-binding the lead to its old pipeline.

    The durable Hosted job already identifies the exact inbound account and lead.
    A CRM move to another organization-owned pipeline must not invalidate that
    WhatsApp thread. Organization/pipeline/stage/lead switches are still checked
    by the canonical permission service, while the Hosted connection switch is
    checked on the actual account that received the customer message.
    """
    from apps.ai_engagement.services.ai_permissions import AIPermissionService
    from apps.channels.models import WhatsAppAccount, WhatsAppMessage
    from apps.crm.models import Lead
    from services.channels.hosted_whatsapp_service import get_session_settings

    lead = Lead.objects.select_related(
        "organization", "pipeline", "stage"
    ).get(pk=lead.pk)

    if account.organization_id != lead.organization_id:
        return "whatsapp_account_organization_mismatch"
    if account.connection_type != WhatsAppAccount.ConnectionType.coexisted:
        return "unsupported_whatsapp_connection_type"
    if not account.is_active:
        return "whatsapp_account_inactive"
    if account.status != WhatsAppAccount.Status.CONNECTED:
        return "whatsapp_account_not_connected"

    latest_for_account = (
        WhatsAppMessage.objects.filter(
            organization=lead.organization,
            account=account,
            lead=lead,
            direction=WhatsAppMessage.Direction.INBOUND,
        )
        .order_by("-created_at", "-id")
        .first()
    )
    if latest_for_account is None:
        return "conversation_whatsapp_account_missing"

    decision = AIPermissionService().evaluate(
        organization=lead.organization,
        lead=lead,
    )
    if not decision.allowed:
        return decision.reason
    if not get_session_settings(account=account).get("ai_auto_reply"):
        return "ai_auto_reply_disabled"
    return ""


def install_ai_orchestration_hooks() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from services.channels import hosted_automation_service, whatsapp_service

    def debounced_queue_engagement(*, lead_id):
        from apps.ai_engagement.services.execution_tracker import queue_api_engagement
        return queue_api_engagement(lead_id=lead_id)

    def background_summary_is_signal_owned(*, lead_id):
        # apps.ai_engagement.background_signals owns throttled summary and
        # qualification refreshes for both Meta and Hosted conversations.
        return None

    def deterministic_reply_intent(*, lead, body):
        """Never advance a CRM stage from a generic yes/interested keyword."""
        text = _normalized_text(body)
        if not text:
            return

        # A normal negative answer may be a qualification answer (for example
        # "No" to previous experience), so it must not disable AI or move a
        # stage. Preserve only the legacy review note for clearly negative
        # conversational intent.
        if classify_reply(body) == Intent.NEGATIVE:
            notes = lead.notes or ""
            marker = "[WhatsApp] Lead replied negatively -- needs review."
            if marker not in notes:
                lead.notes = f"{notes}\n{marker}".strip()
                lead.save(update_fields=["notes", "updated_at"])

    whatsapp_service._queue_whatsapp_engagement = debounced_queue_engagement
    whatsapp_service._queue_internal_conversation_summary = (
        background_summary_is_signal_owned
    )
    whatsapp_service._apply_reply_intent = deterministic_reply_intent

    # Hosted durable jobs already deduplicate by source message. Use the same
    # short debounce as Meta instead of the legacy one-minute response delay.
    hosted_automation_service.AI_RESPONSE_DELAY_SECONDS = _debounce_seconds()
    hosted_automation_service.hosted_ai_block_reason = (
        _conversation_bound_hosted_block_reason
    )

    # Celery may already have imported the task module before Django app-ready
    # hooks run. Refresh that module-level reference as well without importing it
    # eagerly and creating an app-startup cycle.
    hosted_tasks = sys.modules.get("apps.hosted_automation.tasks")
    if hosted_tasks is not None:
        hosted_tasks.hosted_ai_block_reason = _conversation_bound_hosted_block_reason

    _INSTALLED = True