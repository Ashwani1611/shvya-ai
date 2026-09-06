"""Persist CRM events without network calls from request transactions."""

import uuid

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from apps.channels.models import WhatsAppMessage
from apps.crm.models import Lead, LeadCall
from apps.followups.models import LeadSequenceState
from services.triggers.evaluator import emit


@receiver(pre_save, sender=Lead)
def capture_stage(sender, instance, raw=False, **kwargs):
    if raw:
        return
    fields = kwargs.get("update_fields")
    if fields is not None and not set(fields).intersection(
        {"stage", "stage_id", "pipeline", "pipeline_id"}
    ):
        instance._trigger_stage_changed = False
        return
    old = (
        sender.objects.filter(id=instance.id).values("stage_id", "pipeline_id").first()
    )
    instance._trigger_stage_changed = old is not None and (
        old["stage_id"] != instance.stage_id
        or old["pipeline_id"] != instance.pipeline_id
    )


@receiver(post_save, sender=Lead)
def lead_event(sender, instance, created, raw=False, **kwargs):
    if raw:
        return
    if created:
        emit(instance, "lead_created", f"lead-created:{instance.id}")
    elif getattr(instance, "_trigger_stage_changed", False):
        # Some legacy CRM writers save only stage; keep the dwell clock reliable.
        entry = timezone.now()
        sender.objects.filter(
            id=instance.id, organization_id=instance.organization_id
        ).update(stage_entered_at=entry)
        instance.stage_entered_at = entry
        emit(
            instance,
            "stage_moved",
            f"stage:{instance.id}:{uuid.uuid4()}",
            {"stage": str(instance.stage_id), "pipeline": str(instance.pipeline_id)},
        )


@receiver(pre_save, sender=LeadSequenceState)
def capture_sequence(sender, instance, raw=False, **kwargs):
    if not raw:
        instance._trigger_old_status = (
            sender.objects.filter(id=instance.id)
            .values_list("status", flat=True)
            .first()
        )


@receiver(post_save, sender=LeadSequenceState)
def sequence_event(sender, instance, raw=False, **kwargs):
    if (
        not raw
        and instance.status == "completed"
        and getattr(instance, "_trigger_old_status", None) != "completed"
    ):
        emit(
            instance.lead,
            "sequence_ended",
            f"sequence:{instance.id}:{instance.completed_at or uuid.uuid4()}",
            {"sequence": str(instance.sequence_id)},
        )


@receiver(post_save, sender=WhatsAppMessage)
def message_event(sender, instance, created, raw=False, **kwargs):
    if (
        not raw
        and instance.lead_id
        and instance.direction == "outbound"
        and instance.status in ("sent", "delivered", "read")
        and instance.organization_id == instance.lead.organization_id
    ):
        # This identity is persisted at the first successful send, rather than
        # at queue creation; delivery/read receipts cannot reset the clock.
        emit(
            instance.lead,
            "outbound_sent",
            f"outbound-sent:{instance.id}",
            {"message": str(instance.id)},
        )
    if (
        not raw
        and created
        and instance.lead_id
        and instance.direction == "inbound"
        and instance.organization_id == instance.lead.organization_id
    ):
        emit(
            instance.lead, "keyword", f"message:{instance.id}", {"body": instance.body}
        )


@receiver(post_save, sender=LeadCall)
def call_event(sender, instance, created, raw=False, **kwargs):
    if (
        not raw
        and created
        and instance.user_id
        and instance.user.organization_id == instance.lead.organization_id
    ):
        emit(
            instance.lead,
            "call_logged",
            f"call:{instance.id}",
            {"status": instance.status, "manual": True},
        )
