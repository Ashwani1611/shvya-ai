"""AI engagement signal hooks.

WhatsApp inbound AI engagement is intentionally queued by the channel service
after the inbound transaction commits. Do not also queue inbound messages from
a ``WhatsAppMessage`` post-save signal because that would duplicate replies.

These hooks maintain application-controlled qualification state and make queued
AI sends fail closed when current permissions change before dispatch.
"""

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from apps.ai_engagement.services.ai_permissions import (
    AIPermissionError,
    AIPermissionService,
)
from apps.ai_engagement.services.qualification_state import (
    attributes_with_state,
    ensure_state,
    mark_in_progress,
    state_after_stage_change,
)
from apps.channels.models import WhatsAppMessage
from apps.crm.models import Lead


@receiver(pre_save, sender=Lead)
def prepare_qualification_state_for_stage_change(sender, instance, **kwargs):
    """Capture deterministic qualification state before any stage mutation."""

    if not instance.pk or not instance.stage_id:
        return

    old_stage_name = (
        Lead.objects.filter(pk=instance.pk)
        .values_list("stage__name", flat=True)
        .first()
    )
    new_stage_name = getattr(instance.stage, "name", "")

    if not old_stage_name or old_stage_name == new_stage_name:
        return

    state = state_after_stage_change(
        lead=instance,
        old_stage_name=old_stage_name,
        new_stage_name=new_stage_name,
    )
    instance._shvya_qualification_attributes = attributes_with_state(
        instance,
        state,
    )


@receiver(post_save, sender=Lead)
def persist_qualification_state_for_lead(sender, instance, created, **kwargs):
    """Persist state even when the caller saved the stage with update_fields."""

    attributes = getattr(instance, "_shvya_qualification_attributes", None)

    if attributes is None or attributes == (instance.attributes or {}):
        return

    Lead.objects.filter(pk=instance.pk).update(attributes=attributes)
    instance.attributes = attributes
    if hasattr(instance, "_shvya_qualification_attributes"):
        delattr(instance, "_shvya_qualification_attributes")


@receiver(post_save, sender=WhatsAppMessage)
def maintain_qualification_state_from_whatsapp(sender, instance, **kwargs):
    """
    Maintain qualification state without owning WhatsApp task scheduling.

    Inbound messages ensure the backend-owned mode/state exists. Once an AI
    qualification reply is actually queued, the state moves from NOT_STARTED
    to IN_PROGRESS. A queued AI message is also cancelled if the organization,
    current stage, lead, or pipeline-linked WhatsApp number is no longer
    eligible at this exact point in time.
    """

    if not instance.lead_id:
        return

    lead = (
        Lead.objects.select_related(
            "organization",
            "pipeline",
            "stage",
        )
        .filter(pk=instance.lead_id)
        .first()
    )
    if lead is None:
        return

    if instance.direction == WhatsAppMessage.Direction.INBOUND:
        ensure_state(lead)
        return

    if instance.status != WhatsAppMessage.Status.QUEUED:
        return

    payload = instance.raw_payload if isinstance(instance.raw_payload, dict) else {}
    if not payload.get("shvya_ai"):
        return

    try:
        permission = AIPermissionService().evaluate(
            organization=lead.organization,
            lead=lead,
        )
    except AIPermissionError:
        permission = None

    if permission is None or not permission.allowed:
        reason = (
            permission.reason
            if permission is not None
            else "ai_permission_evaluation_failed"
        )
        WhatsAppMessage.objects.filter(
            pk=instance.pk,
            status=WhatsAppMessage.Status.QUEUED,
        ).update(
            status=WhatsAppMessage.Status.FAILED,
            error=f"AI send cancelled: {reason}",
            updated_at=timezone.now(),
        )
        return

    mark_in_progress(lead)
