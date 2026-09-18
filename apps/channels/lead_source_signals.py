"""Refresh Hosted inbox metadata without rewriting a lead's creation source.

Creation services record the transport explicitly when the lead is created.
"""

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.channels.models import WhatsAppMessage
from apps.crm.models import Lead


@receiver(post_save, sender=Lead)
def refresh_hosted_lead_metadata(sender, instance, created, update_fields=None, **kwargs):
    if update_fields and not {"name", "stage", "stage_id", "phone"}.intersection(update_fields):
        return
    from django.db import transaction
    from django.db.models import Q
    from services.channels.hosted_chat_service import broadcast_hosted_chat_refresh

    lead_id, org_id, phone = instance.pk, instance.organization_id, instance.phone

    def publish():
        accounts = WhatsAppMessage.objects.filter(
            organization_id=org_id, account__connection_type="hosted", account__is_active=True,
        ).filter(
            Q(lead_id=lead_id)
            | Q(direction="inbound", from_number=phone)
            | Q(direction="outbound", to_number=phone)
        ).order_by().values_list("account_id", flat=True).distinct()
        for account_id in accounts:
            broadcast_hosted_chat_refresh(account_id=account_id, reason="lead", chat_key=phone)

    transaction.on_commit(publish, robust=True)
