"""Low-cardinality messaging throughput metrics shared across replicas."""

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.core.observability import increment

from .instagram_models import InstagramMessage
from .models import WhatsAppMessage


@receiver(post_save, sender=WhatsAppMessage, dispatch_uid="whatsapp-throughput-metric")
def record_whatsapp_throughput(sender, instance, created, **kwargs):
    if getattr(instance.account, "is_operations_test", False):
        return
    if created:
        increment(
            "messaging.messages_created",
            labels={"provider": "whatsapp", "direction": instance.direction},
        )


@receiver(post_save, sender=InstagramMessage, dispatch_uid="instagram-throughput-metric")
def record_instagram_throughput(sender, instance, created, **kwargs):
    if created:
        increment(
            "messaging.messages_created",
            labels={"provider": "instagram", "direction": instance.direction},
        )
