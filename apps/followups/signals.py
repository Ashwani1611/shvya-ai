"""Runtime hooks for Auto Follow-ups."""

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.channels.models import WhatsAppMessage
from apps.followups.models import FollowupStep


@receiver(post_save, sender=WhatsAppMessage)
def delay_followup_for_whatsapp_activity(sender, instance, created, **kwargs):
    if not created or not instance.lead_id:
        return

    payload = instance.raw_payload if isinstance(instance.raw_payload, dict) else {}
    if payload.get("shvya_auto_followup"):
        # A sequence-created outbound row is the action being executed, not a
        # conversation event that should delay itself.
        return

    lead_id = instance.lead_id
    created_at = instance.created_at
    direction = instance.direction

    def apply_delay():
        from apps.crm.models import Lead
        from services.followup_service import register_lead_reply, register_manual_outbound

        lead = Lead.objects.filter(id=lead_id).select_related("organization").first()
        if not lead:
            return
        if direction == WhatsAppMessage.Direction.INBOUND:
            register_lead_reply(lead=lead, at=created_at)
        elif direction == WhatsAppMessage.Direction.OUTBOUND:
            register_manual_outbound(lead=lead, at=created_at)

    transaction.on_commit(apply_delay)


def _schedule_recalculation(sequence_id):
    def recalculate():
        from apps.followups.models import FollowupSequence
        from services.followup_service import _recalculate_active_states

        sequence = FollowupSequence.objects.filter(id=sequence_id).first()
        if sequence:
            _recalculate_active_states(sequence)

    transaction.on_commit(recalculate)


@receiver(post_save, sender=FollowupStep)
def recalculate_after_step_save(sender, instance, **kwargs):
    _schedule_recalculation(instance.sequence_id)


@receiver(post_delete, sender=FollowupStep)
def recalculate_after_step_delete(sender, instance, **kwargs):
    _schedule_recalculation(instance.sequence_id)
