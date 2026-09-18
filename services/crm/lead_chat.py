"""Resolve the current pipeline's connected number without guessing a transport."""
import re
from urllib.parse import urlencode
from django.core.exceptions import ValidationError
from django.urls import reverse
from apps.channels.models import WhatsAppAccount


def pipeline_chat_account(lead):
    value = str(lead.pipeline.phone_number or "").strip()
    if not value:
        raise ValidationError("This pipeline has no linked WhatsApp number. Link a connected number in pipeline settings.")
    digits = re.sub(r"\D", "", value)
    country = re.sub(r"\D", "", lead.pipeline.country_code or "")
    numbers = {digits}
    if country and not value.startswith("+") and not digits.startswith(country):
        numbers.add(country + digits)
    accounts = WhatsAppAccount.objects.filter(
        organization_id=lead.organization_id, is_active=True, status="connected",
    ).only("id", "organization_id", "connection_type", "display_phone_number", "phone_number_id", "status", "is_active")
    matches = [a for a in accounts if value == a.phone_number_id or
               (digits and re.sub(r"\D", "", a.display_phone_number or "") in numbers)]
    if len(matches) != 1:
        raise ValidationError("The pipeline's number is disconnected or has multiple connections. Review its WhatsApp connection.")
    return matches[0]


def lead_chat_url(lead):
    account = pipeline_chat_account(lead)
    if account.connection_type == WhatsAppAccount.ConnectionType.API:
        return reverse("whatsapp-chat-detail", args=[lead.pk]) + "?" + urlencode({"account": account.pk})
    return reverse("whatsapp-hosted-session-chats", args=[account.pk]) + "?" + urlencode({"chat": lead.phone})
