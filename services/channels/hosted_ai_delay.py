"""Install Hosted AI delay/delivery routing without changing Meta AI calls."""

_INSTALLED = False
_ORIGINAL_AI_DELAY = None
_ORIGINAL_MESSAGE_DELAY = None


def install_hosted_ai_delay_dispatch():
    global _INSTALLED, _ORIGINAL_AI_DELAY, _ORIGINAL_MESSAGE_DELAY
    if _INSTALLED:
        return

    from apps.ai_engagement.tasks import generate_ai_engagement_response
    from apps.channels.models import WhatsAppMessage
    from apps.channels.tasks import send_whatsapp_message_task
    from apps.crm.models import Lead
    from services.channels.hosted_automation_service import enqueue_ai_engagement

    _ORIGINAL_AI_DELAY = generate_ai_engagement_response.delay
    _ORIGINAL_MESSAGE_DELAY = send_whatsapp_message_task.delay

    def provider_aware_ai_delay(lead_id, *args, **kwargs):
        try:
            lead = Lead.objects.select_related("organization").get(id=lead_id)
        except Lead.DoesNotExist:
            return _ORIGINAL_AI_DELAY(lead_id, *args, **kwargs)

        latest = (
            WhatsAppMessage.objects.filter(lead=lead, organization=lead.organization)
            .select_related("account")
            .order_by("-created_at", "-id")
            .first()
        )
        if (
            latest
            and latest.direction == WhatsAppMessage.Direction.INBOUND
            and latest.account.connection_type == "hosted"
        ):
            return enqueue_ai_engagement(
                account=latest.account,
                lead=lead,
                source_message=latest,
            )
        return _ORIGINAL_AI_DELAY(lead_id, *args, **kwargs)

    def provider_aware_message_delay(message_id, *args, **kwargs):
        """Hosted AI delivery is owned by HostedAutomationJob.

        The canonical AI function queues the WhatsAppMessage and schedules the
        normal sender after commit. Suppress only that hosted AI sender call so
        the durable job can keep the exact queued message through a 12-hour
        Account Health cooldown without racing a second worker. All Meta sends,
        hosted agent sends, and non-AI hosted sends keep the canonical task.
        """
        message = (
            WhatsAppMessage.objects.select_related("account")
            .filter(id=message_id)
            .first()
        )
        payload = message.raw_payload if message and isinstance(message.raw_payload, dict) else {}
        if (
            message
            and message.account.connection_type == "hosted"
            and payload.get("shvya_ai")
        ):
            return None
        return _ORIGINAL_MESSAGE_DELAY(message_id, *args, **kwargs)

    generate_ai_engagement_response.delay = provider_aware_ai_delay
    send_whatsapp_message_task.delay = provider_aware_message_delay
    _INSTALLED = True
