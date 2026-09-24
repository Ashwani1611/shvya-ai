"""Verified inbox identities and explicit CRM creation with canonical AI handoff."""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from apps.channels.models import WhatsAppMessage
from apps.crm.models import Lead
from apps.crm.models.lead import normalize_phone
from services.crm.lead_filter_service import accessible_pipelines
from services.crm.lead_service import create_lead
from services.followup_service import resolve_linked_whatsapp_account


def chat_identity(*, account, chat):
    if account.connection_type == "hosted":
        from services.channels.hosted_chat_service import build_hosted_chat_snapshot, _selected_thread_queryset
        snapshot = build_hosted_chat_snapshot(account=account, selected_chat=chat, auto_select=False)
        row = snapshot.get("selected_contact") or {}
        messages = _selected_thread_queryset(account, snapshot["selected_chat"], row.get("raw_chat_ids"))
        phone = row.get("phone") if not row.get("is_group") else ""
        return phone or "", row.get("name", ""), messages
    message = WhatsAppMessage.objects.filter(pk=chat, account=account, organization=account.organization).first()
    if not message:
        raise ValidationError("Conversation not found.")
    raw = message.from_number if message.direction == "inbound" else message.to_number
    if not raw or not raw.lstrip("+").isdigit():
        raise ValidationError("This conversation has no verified phone number.")
    phone = normalize_phone("+" + raw.lstrip("+"))
    variants = [phone, phone.lstrip("+")]
    messages = WhatsAppMessage.objects.filter(account=account, organization=account.organization).filter(
        Q(direction="inbound", from_number__in=variants) | Q(direction="outbound", to_number__in=variants)
    )
    return phone, phone, messages


def creation_pipelines(*, user, account):
    result = []
    for pipeline in accessible_pipelines(user).filter(is_active=True):
        candidate = Lead(organization=user.organization, pipeline=pipeline)
        sender = resolve_linked_whatsapp_account(lead=candidate, connection_type=account.connection_type)
        if sender and sender.pk == account.pk:
            result.append(pipeline)
    return result


def _queue_created_lead_engagement(*, organization_id, account_id, lead_id, source_message_id):
    """Resume the channel's existing AI path after an explicit lead link.

    QuerySet.update deliberately does not emit message post_save signals, so a
    lead created from the inbox would otherwise miss the same engagement handoff
    that a normally persisted live inbound message receives.
    """
    from apps.channels.models import WhatsAppAccount

    account = WhatsAppAccount.objects.filter(
        pk=account_id,
        organization_id=organization_id,
        is_active=True,
    ).first()
    if account is None:
        return

    if account.connection_type == "hosted":
        # Ordinary Hosted history sync must never auto-engage. This path is
        # different: an authenticated user explicitly clicked Create Lead for
        # this exact conversation, so activate one source-bound AI turn while
        # preserving the existing permission, routing, debounce and idempotency
        # checks downstream.
        from apps.hosted_automation.signals import _queue_hosted_ai_from_persisted_message

        _queue_hosted_ai_from_persisted_message(
            source_message_id,
            allow_history=True,
        )
        return

    from services.channels.hosted_whatsapp_service import get_session_settings

    if not get_session_settings(account=account).get("ai_auto_reply"):
        return

    # Meta/API automation owns the durable execution marker and duplicate-turn
    # protection. Keep the existing lead-only task contract.
    from services.channels.whatsapp_service import _queue_whatsapp_engagement

    _queue_whatsapp_engagement(
        lead_id=str(lead_id),
        source_message_id=source_message_id,
    )


@transaction.atomic
def create_chat_lead(*, user, account, chat, name, pipeline_id):
    # Lock the account to serialize two simultaneous Create clicks for this inbox.
    type(account).objects.select_for_update().get(pk=account.pk, organization=user.organization)
    phone, _, messages = chat_identity(account=account, chat=chat)
    if not phone:
        raise ValidationError("A verified individual phone number is required. Group chats cannot become leads.")
    allowed = accessible_pipelines(user)
    lead = Lead.objects.filter(organization=user.organization, phone=phone).first()
    if lead and not allowed.filter(pk=lead.pipeline_id).exists():
        raise ValidationError("This contact is in a pipeline you cannot access.")

    created = lead is None
    if created:
        pipeline = next((p for p in creation_pipelines(user=user, account=account) if str(p.pk) == pipeline_id), None)
        if not pipeline:
            raise ValidationError("Choose a pipeline linked to this chat account.")
        stage = pipeline.stages.filter(is_active=True).order_by("display_order").first()
        if not stage:
            raise ValidationError("This pipeline needs an active stage before creating a lead.")
        lead = create_lead(organization=user.organization, pipeline=pipeline, stage=stage,
                           name=name.strip(), phone=phone, send_welcome=False,
                           lead_source="whatsapp" if account.connection_type == "hosted" else "whatsapp_api")

    linked_count = messages.filter(lead__isnull=True).update(lead=lead)

    # Explicit linking happens after the inbound message already exists. Rejoin
    # the same post-commit AI path whenever this action actually attaches an
    # unlinked conversation, even if a matching CRM Lead appeared just before
    # the click. The downstream execution tracker / Hosted one-to-one job makes
    # this safe against retries and duplicate Create clicks.
    if linked_count:
        source_message_id = (
            messages.filter(
                lead=lead,
                direction=WhatsAppMessage.Direction.INBOUND,
            )
            .order_by("-created_at", "-id")
            .values_list("pk", flat=True)
            .first()
        )
        if source_message_id:
            transaction.on_commit(
                lambda: _queue_created_lead_engagement(
                    organization_id=user.organization_id,
                    account_id=account.pk,
                    lead_id=lead.pk,
                    source_message_id=source_message_id,
                )
            )

    return lead
