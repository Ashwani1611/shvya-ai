"""Verified inbox identities and explicit CRM creation, without sending messages."""
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
    if not lead:
        pipeline = next((p for p in creation_pipelines(user=user, account=account) if str(p.pk) == pipeline_id), None)
        if not pipeline:
            raise ValidationError("Choose a pipeline linked to this chat account.")
        stage = pipeline.stages.filter(is_active=True).order_by("display_order").first()
        if not stage:
            raise ValidationError("This pipeline needs an active stage before creating a lead.")
        lead = create_lead(organization=user.organization, pipeline=pipeline, stage=stage,
                           name=name.strip(), phone=phone, send_welcome=False,
                           lead_source="whatsapp" if account.connection_type == "hosted" else "whatsapp_api")
    messages.filter(lead__isnull=True).update(lead=lead)
    return lead
