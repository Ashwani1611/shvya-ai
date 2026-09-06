from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.crm.models import Lead
from apps.integrations.models import WebhookConfiguration, WebhookDelivery
from apps.integrations.services.webhook import build_lead_webhook_payload


@receiver(post_save, sender=Lead, dispatch_uid="integrations.queue_lead_webhook")
def queue_lead_webhook(sender, instance, created, raw=False, **kwargs):
    """Persist a webhook event in the same DB transaction, then deliver on commit."""
    if raw or not instance.organization_id:
        return

    webhook = (
        WebhookConfiguration.objects.filter(
            organization_id=instance.organization_id,
            is_enabled=True,
        )
        .only("id", "organization_id", "endpoint_url", "encrypted_secret")
        .first()
    )

    if webhook is None or not webhook.endpoint_url or not webhook.has_secret:
        return

    event_type = (
        WebhookDelivery.EventType.CREATE
        if created
        else WebhookDelivery.EventType.UPDATE
    )

    delivery = WebhookDelivery.objects.create(
        webhook=webhook,
        organization_id=instance.organization_id,
        lead_id=instance.id,
        event_type=event_type,
        payload=build_lead_webhook_payload(instance, event_type),
    )

    delivery_id = str(delivery.id)

    def enqueue():
        from apps.integrations.tasks import deliver_webhook_task

        deliver_webhook_task.delay(delivery_id)

    transaction.on_commit(enqueue)
