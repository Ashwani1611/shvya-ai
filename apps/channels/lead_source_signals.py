"""Transport-specific lead source normalization.

Hosted WhatsApp creates the Lead immediately before its first inbound message is
persisted. Keep the existing transport service isolated while ensuring the final
CRM source reflects the transport that actually created the lead.
"""

from datetime import timedelta

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead


@receiver(post_save, sender=WhatsAppMessage)
def set_hosted_created_lead_source(sender, instance, created, **kwargs):
    if not created or not instance.lead_id:
        return
    if instance.direction != WhatsAppMessage.Direction.INBOUND:
        return
    if instance.account.connection_type != WhatsAppAccount.ConnectionType.HOSTED:
        return

    lead = instance.lead
    if lead.lead_source != "whatsapp_api":
        return

    # Hosted auto-lead creation happens synchronously just before this first
    # inbound message. Do not relabel an older lead that later starts talking
    # through a Hosted account.
    if not lead.created_at or not instance.created_at:
        return
    if instance.created_at - lead.created_at > timedelta(seconds=10):
        return

    has_older_message = WhatsAppMessage.objects.filter(
        lead_id=lead.id,
        created_at__lt=instance.created_at,
    ).exclude(pk=instance.pk).exists()
    if has_older_message:
        return

    Lead.objects.filter(pk=lead.pk, lead_source="whatsapp_api").update(
        lead_source="whatsapp"
    )
    lead.lead_source = "whatsapp"
