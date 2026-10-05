from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.crm.models import Lead


@receiver(post_save, sender=Lead, dispatch_uid="telephony_reconcile_lead_calls")
def link_earlier_calls(sender, instance, raw=False, update_fields=None, **kwargs):
    if raw or not instance.phone:
        return
    if update_fields is not None and "phone" not in update_fields:
        return
    from .services import reconcile_lead_calls

    # Ingest may itself create this lead while holding the call lock. Wait for
    # that transaction to finish before attaching earlier, unlinked calls.
    transaction.on_commit(lambda lead_id=instance.id: reconcile_lead_calls(lead_id))
