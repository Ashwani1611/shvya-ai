"""Install the Hosted Account ~60 second AI delay without changing Meta AI calls."""

_INSTALLED = False
_ORIGINAL_DELAY = None


def install_hosted_ai_delay_dispatch():
    global _INSTALLED, _ORIGINAL_DELAY
    if _INSTALLED:
        return

    from apps.ai_engagement.tasks import generate_ai_engagement_response
    from apps.channels.models import WhatsAppMessage
    from apps.crm.models import Lead
    from services.channels.hosted_automation_service import enqueue_ai_engagement

    _ORIGINAL_DELAY = generate_ai_engagement_response.delay

    def provider_aware_delay(lead_id, *args, **kwargs):
        try:
            lead = Lead.objects.select_related("organization").get(id=lead_id)
        except Lead.DoesNotExist:
            return _ORIGINAL_DELAY(lead_id, *args, **kwargs)

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
        return _ORIGINAL_DELAY(lead_id, *args, **kwargs)

    generate_ai_engagement_response.delay = provider_aware_delay
    _INSTALLED = True
