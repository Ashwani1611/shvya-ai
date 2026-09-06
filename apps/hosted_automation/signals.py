from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.channels.models import WhatsAppMessage


@receiver(post_save, sender=WhatsAppMessage, dispatch_uid="hosted_automation_message_state")
def hosted_message_state(sender, instance, created, **kwargs):
    if not created or instance.account.connection_type != "hosted" or not instance.lead_id:
        return

    if instance.direction == WhatsAppMessage.Direction.INBOUND:
        def apply_inbound_delay():
            from services.channels.hosted_automation_service import register_hosted_lead_reply

            register_hosted_lead_reply(
                account=instance.account,
                lead=instance.lead,
                at=instance.created_at,
            )

        transaction.on_commit(apply_inbound_delay)
