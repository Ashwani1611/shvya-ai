"""Hard safety rail for outbound messaging from the staging environment.

Production behavior is unchanged. In staging, sends are blocked by default and
can only be enabled for explicitly allowlisted test recipients.
"""

from __future__ import annotations

from functools import wraps
import re

from django.conf import settings


_INSTALLED = False


def _recipient_key(value) -> str:
    raw = str(value or "").strip()
    digits = re.sub(r"\D", "", raw)
    return digits or raw


def staging_outbound_allowed(recipient) -> bool:
    if str(getattr(settings, "APP_ENV", "production")).lower() != "staging":
        return True
    if not bool(getattr(settings, "OUTBOUND_MESSAGING_ENABLED", False)):
        return False

    allowed = getattr(settings, "STAGING_ALLOWED_RECIPIENTS", set()) or set()
    allowed_keys = {_recipient_key(value) for value in allowed if _recipient_key(value)}
    key = _recipient_key(recipient)
    return bool(key and key in allowed_keys)


def install_staging_outbound_safety() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from apps.channels.providers.whatsapp import WhatsAppAPIError, WhatsAppClient
    from services.channels import hosted_whatsapp_transport, instagram_service
    from services.channels.whatsapp_service import WhatsAppSendError

    original_text = WhatsAppClient.send_text_message
    original_template = WhatsAppClient.send_template_message
    original_media = WhatsAppClient.send_media_message

    def _guard_whatsapp(recipient):
        if not staging_outbound_allowed(recipient):
            raise WhatsAppAPIError(
                "Staging outbound messaging blocked. Add the test recipient to "
                "STAGING_ALLOWED_RECIPIENTS and enable OUTBOUND_MESSAGING_ENABLED."
            )

    @wraps(original_text)
    def send_text_message(self, to, body, preview_url=False):
        _guard_whatsapp(to)
        return original_text(self, to=to, body=body, preview_url=preview_url)

    @wraps(original_template)
    def send_template_message(
        self,
        to,
        template_name,
        language_code="en_US",
        components=None,
    ):
        _guard_whatsapp(to)
        return original_template(
            self,
            to=to,
            template_name=template_name,
            language_code=language_code,
            components=components,
        )

    @wraps(original_media)
    def send_media_message(
        self,
        *,
        to,
        media_type,
        media_id=None,
        media_url=None,
        caption=None,
        filename=None,
    ):
        _guard_whatsapp(to)
        return original_media(
            self,
            to=to,
            media_type=media_type,
            media_id=media_id,
            media_url=media_url,
            caption=caption,
            filename=filename,
        )

    WhatsAppClient.send_text_message = send_text_message
    WhatsAppClient.send_template_message = send_template_message
    WhatsAppClient.send_media_message = send_media_message

    original_hosted_send = hosted_whatsapp_transport.send_hosted_message

    @wraps(original_hosted_send)
    def send_hosted_message(*, message, defer_on_pause=True):
        if not staging_outbound_allowed(message.to_number):
            raise WhatsAppSendError(
                "Staging Hosted WhatsApp send blocked for a non-allowlisted recipient."
            )
        return original_hosted_send(message=message, defer_on_pause=defer_on_pause)

    hosted_whatsapp_transport.send_hosted_message = send_hosted_message

    original_instagram_send = instagram_service.send_queued_message

    @wraps(original_instagram_send)
    def send_queued_message(message):
        recipient = getattr(message, "recipient_id", "")
        if not staging_outbound_allowed(recipient):
            raise instagram_service.InstagramAPIError(
                "Staging Instagram send blocked for a non-allowlisted recipient."
            )
        return original_instagram_send(message)

    instagram_service.send_queued_message = send_queued_message
    _INSTALLED = True
