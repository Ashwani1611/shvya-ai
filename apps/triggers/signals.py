from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from apps.channels.models import WhatsAppMessage
from apps.crm.models import Lead
from apps.triggers.models import SmartTrigger
from services.triggers.publisher import queue_trigger_event
from services.triggers.runtime import trigger_signals_suppressed


_TRACKED_LEAD_FIELDS = (
    "name",
    "phone",
    "email",
    "notes",
    "attributes",
    "ai_enabled",
    "lead_source",
    "pipeline_id",
    "stage_id",
)


def _publish_after_commit(*, organization_id, lead_id, event_type, payload):
    transaction.on_commit(
        lambda: queue_trigger_event(
            organization_id=organization_id,
            lead_id=lead_id,
            event_type=event_type,
            payload=payload,
        )
    )


@receiver(pre_save, sender=Lead)
def remember_lead_state(sender, instance, raw=False, **kwargs):
    if raw or trigger_signals_suppressed() or not instance.pk:
        return
    instance._smart_trigger_old_state = (
        sender.objects.filter(pk=instance.pk).values(*_TRACKED_LEAD_FIELDS).first()
    )


@receiver(post_save, sender=Lead)
def publish_lead_events(sender, instance, created, raw=False, **kwargs):
    if raw or trigger_signals_suppressed() or not instance.organization_id:
        return

    if created:
        _publish_after_commit(
            organization_id=instance.organization_id,
            lead_id=instance.id,
            event_type=SmartTrigger.EventType.LEAD_CREATED,
            payload={
                "lead_source": instance.lead_source,
                "pipeline_id": str(instance.pipeline_id) if instance.pipeline_id else "",
                "stage_id": str(instance.stage_id) if instance.stage_id else "",
            },
        )
        return

    old = getattr(instance, "_smart_trigger_old_state", None)
    if not old:
        return

    changed_fields = [
        field
        for field in _TRACKED_LEAD_FIELDS
        if old.get(field) != getattr(instance, field)
    ]
    if not changed_fields:
        return

    if "stage_id" in changed_fields:
        _publish_after_commit(
            organization_id=instance.organization_id,
            lead_id=instance.id,
            event_type=SmartTrigger.EventType.LEAD_STAGE_CHANGED,
            payload={
                "from_stage_id": str(old.get("stage_id") or ""),
                "to_stage_id": str(instance.stage_id or ""),
                "changed_fields": changed_fields,
            },
        )

    _publish_after_commit(
        organization_id=instance.organization_id,
        lead_id=instance.id,
        event_type=SmartTrigger.EventType.LEAD_UPDATED,
        payload={"changed_fields": changed_fields},
    )


@receiver(post_save, sender=WhatsAppMessage)
def publish_whatsapp_received(sender, instance, created, raw=False, **kwargs):
    if raw or not created or trigger_signals_suppressed():
        return
    if instance.direction != WhatsAppMessage.Direction.INBOUND or not instance.lead_id:
        return

    _publish_after_commit(
        organization_id=instance.organization_id,
        lead_id=instance.lead_id,
        event_type=SmartTrigger.EventType.WHATSAPP_RECEIVED,
        payload={
            "message_id": str(instance.id),
            "message_body": instance.body or "",
            "from_number": instance.from_number or "",
            "to_number": instance.to_number or "",
        },
    )
