"""Resilient Meta embedded-signup completion for WhatsApp accounts.

The account connection itself and the WABA webhook subscription are two separate
steps. A webhook subscription failure must not make a successfully-authorized
WhatsApp number disappear from Connected Numbers.
"""

import json
import logging

from django.conf import settings
from django.db import transaction

from apps.channels.models import WhatsAppAccount
from apps.channels.providers import whatsapp as whatsapp_provider
from apps.channels.providers.whatsapp import WhatsAppAPIError

logger = logging.getLogger(__name__)


class EmbeddedSignupError(Exception):
    """Raised when Meta authorization/number lookup cannot be completed."""


def _meta_error_text(exc):
    """Return a short useful Meta error without exposing tokens or raw payloads."""
    body = getattr(exc, "response_body", None)
    if body:
        try:
            data = json.loads(body)
            error = data.get("error") or {}
            message = error.get("message")
            details = (error.get("error_data") or {}).get("details")
            if details:
                return str(details)
            if message:
                return str(message)
        except (TypeError, ValueError):
            pass
    return str(exc)


def complete_embedded_signup(*, organization, code, waba_id, phone_number_id):
    """Authorize Meta, persist the number, then try webhook subscription.

    Returns ``(account, warning)``. ``warning`` is empty when webhook
    subscription succeeded. The WhatsApp account remains connected and visible
    even when the subscription call fails, because sending and account ownership
    were already successfully authorized by Meta.
    """
    if not settings.META_APP_ID or not settings.META_APP_SECRET:
        raise EmbeddedSignupError(
            "Embedded signup is not configured on this server yet."
        )

    try:
        access_token = whatsapp_provider.exchange_code_for_access_token(
            app_id=settings.META_APP_ID,
            app_secret=settings.META_APP_SECRET,
            code=code,
        )
        phone_details = whatsapp_provider.get_phone_number_details(
            phone_number_id=phone_number_id,
            access_token=access_token,
        )
    except WhatsAppAPIError as exc:
        raise EmbeddedSignupError(_meta_error_text(exc)) from exc

    # Persist first. Reconnecting a previously disconnected number must also
    # reactivate it; otherwise account_list.html intentionally hides the row.
    with transaction.atomic():
        account, _created = WhatsAppAccount.objects.update_or_create(
            organization=organization,
            phone_number_id=phone_number_id,
            defaults={
                "connection_type": WhatsAppAccount.ConnectionType.API,
                "waba_id": waba_id,
                "display_phone_number": phone_details.get("display_phone_number", ""),
                "business_name": phone_details.get("verified_name", ""),
                "access_token": access_token,
                "status": WhatsAppAccount.Status.CONNECTED,
                "is_active": True,
            },
        )

    warning = ""
    try:
        whatsapp_provider.subscribe_app_to_waba(
            waba_id=waba_id,
            access_token=access_token,
        )
    except WhatsAppAPIError as exc:
        reason = _meta_error_text(exc)
        logger.warning(
            "Embedded signup connected account %s but WABA subscription failed: %s",
            account.id,
            reason,
        )
        warning = (
            "WhatsApp connected successfully, but message notifications could not "
            f"be subscribed yet: {reason}. You can retry the refresh action from "
            "Connected Numbers after fixing the Meta permission/setup."
        )

    return account, warning
