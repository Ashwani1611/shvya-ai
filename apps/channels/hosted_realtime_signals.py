"""Publish live Hosted message saves from HTTP callbacks and Celery workers."""

from functools import partial

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.channels.models import WhatsAppMessage
from services.channels.hosted_chat_realtime import broadcast_committed_message


@receiver(post_save, sender=WhatsAppMessage, dispatch_uid="hosted_inbox_committed_message_v1")
def hosted_message_saved(sender, instance, raw=False, using="default", **kwargs):
    if raw:
        return
    account = instance._state.fields_cache.get("account")
    if account is not None and account.connection_type != "hosted":
        return
    payload = instance.raw_payload if isinstance(instance.raw_payload, dict) else {}
    if payload.get("isHistory") is True:
        return
    # Read the row again after commit: identity/content repair and final status
    # updates can happen after the initial save inside the same transaction.
    transaction.on_commit(
        partial(broadcast_committed_message, instance.pk, using),
        using=using, robust=True,
    )
