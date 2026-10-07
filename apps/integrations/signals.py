from django.db import transaction
from django.db.models.signals import post_save, pre_save, pre_delete
from django.dispatch import receiver

from apps.crm.models import Lead
from apps.integrations.models import WebhookConfiguration, WebhookDelivery
from apps.integrations.services.webhook import build_lead_webhook_payload


@receiver(pre_save, sender=Lead, dispatch_uid="integrations.meta_conversion_previous_stage")
def remember_meta_conversion_stage(sender, instance, raw=False, update_fields=None, **kwargs):
    instance._meta_conversion_context = None
    if raw or instance.is_operations_test or not instance.organization_id:
        return
    if update_fields is not None and not {
        "pipeline", "pipeline_id", "stage", "stage_id", "attributes", "lead_source",
    }.intersection(update_fields):
        return
    from apps.integrations.models import MetaConversionsConfiguration
    configuration = MetaConversionsConfiguration.objects.filter(
        organization_id=instance.organization_id, is_enabled=True,
    ).first()
    if configuration is None:
        return
    previous = Lead.objects.filter(pk=instance.pk, organization_id=instance.organization_id).values(
        "pipeline_id", "stage_id", "attributes__meta_leadgen_id",
    ).first()
    saved_fields = update_fields if update_fields is not None else {"pipeline", "stage", "attributes"}
    meta_id = str((instance.attributes or {}).get("meta_leadgen_id") or "")
    changed = (previous is None or
               ({"pipeline", "pipeline_id"}.intersection(saved_fields) and previous["pipeline_id"] != instance.pipeline_id) or
               ({"stage", "stage_id"}.intersection(saved_fields) and previous["stage_id"] != instance.stage_id) or
               ("attributes" in saved_fields and meta_id and meta_id != str(previous["attributes__meta_leadgen_id"] or "")))
    if not changed:
        return
    from django.utils import timezone
    instance._meta_conversion_context = (configuration, timezone.now(), previous)


@receiver(post_save, sender=Lead, dispatch_uid="integrations.capture_meta_conversion")
def capture_meta_conversion(sender, instance, raw=False, **kwargs):
    context = getattr(instance, "_meta_conversion_context", None)
    instance._meta_conversion_context = None
    if raw or instance.is_operations_test or context is None:
        return
    from apps.integrations.services.meta_conversions import capture_stage_event
    configuration, occurred_at, previous = context
    # update_fields may leave other in-memory lead fields unsaved. Both the
    # transition comparison and payload must use the CRM data actually saved.
    lead = Lead.objects.select_related("pipeline", "stage").filter(
        pk=instance.pk, organization_id=configuration.organization_id,
    ).first()
    if lead is None:
        return
    meta_id = str((lead.attributes or {}).get("meta_leadgen_id") or "")
    changed = (previous is None or previous["pipeline_id"] != lead.pipeline_id or
               previous["stage_id"] != lead.stage_id or
               (meta_id and meta_id != str(previous["attributes__meta_leadgen_id"] or "")))
    if changed:
        capture_stage_event(configuration, lead, occurred_at=occurred_at)


@receiver(post_save, sender=Lead, dispatch_uid="integrations.queue_lead_webhook")
def queue_lead_webhook(sender, instance, created, raw=False, **kwargs):
    """Persist a webhook event in the same DB transaction, then deliver on commit."""
    if raw or instance.is_operations_test or not instance.organization_id:
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


@receiver(pre_delete, sender=Lead, dispatch_uid="integrations.erase_indiamart_buyer_payload")
def erase_indiamart_buyer_payload(sender, instance, using, **kwargs):
    """Keep only query tombstones so retries cannot recreate deleted buyers."""
    from apps.integrations.models import IndiaMartReceipt

    IndiaMartReceipt.objects.using(using).filter(lead_id=instance.pk).update(payload={})
