"""Resilient Meta embedded-signup completion for WhatsApp accounts.

The account connection itself and the WABA webhook subscription are separate
steps. A webhook subscription failure must not make a successfully-authorized
WhatsApp number disappear from Connected Numbers.

The optional ``attempt`` object records safe lifecycle diagnostics. Authorization
codes and raw access-token values are deliberately never stored or logged.
"""

import json
import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.channels.models import WhatsAppAccount
from apps.channels.providers import whatsapp as whatsapp_provider
from apps.channels.providers.whatsapp import WhatsAppAPIError

logger = logging.getLogger(__name__)


class EmbeddedSignupError(Exception):
    """Raised when Meta authorization/number lookup cannot be completed."""

    def __init__(self, message, *, stage="", meta_error_code=""):
        super().__init__(message)
        self.stage = stage
        self.meta_error_code = str(meta_error_code or "")


def _meta_error_details(exc):
    """Return ``(code, message)`` without exposing tokens or raw payloads."""
    body = getattr(exc, "response_body", None)
    if body:
        try:
            data = json.loads(body)
            error = data.get("error") or {}
            message = error.get("message")
            details = (error.get("error_data") or {}).get("details")
            code = error.get("code") or getattr(exc, "status_code", "")
            if details:
                return str(code or ""), str(details)
            if message:
                return str(code or ""), str(message)
        except (TypeError, ValueError):
            pass
    return str(getattr(exc, "status_code", "") or ""), str(exc)


def _update_attempt(attempt, **changes):
    """Safely update an audit attempt when one was supplied."""
    if attempt is None:
        return

    for field, value in changes.items():
        setattr(attempt, field, value)
    attempt.save()


def complete_embedded_signup(
    *,
    organization,
    code,
    waba_id,
    phone_number_id,
    attempt=None,
):
    """Authorize Meta, persist the number, then try webhook subscription.

    Returns ``(account, warning)``. ``warning`` is empty when webhook
    subscription succeeded. The WhatsApp account remains connected and visible
    even when the subscription call fails, because sending/account ownership
    were already successfully authorized by Meta.
    """
    if not settings.META_APP_ID or not settings.META_APP_SECRET:
        raise EmbeddedSignupError(
            "Embedded signup is not configured on this server yet.",
            stage="server_configuration",
        )

    # Step 1: exchange the short-lived authorization code for the token SHVYA
    # needs to call Meta. Never log or persist the code/token in the attempt.
    try:
        access_token = whatsapp_provider.exchange_code_for_access_token(
            app_id=settings.META_APP_ID,
            app_secret=settings.META_APP_SECRET,
            code=code,
        )
    except WhatsAppAPIError as exc:
        error_code, reason = _meta_error_details(exc)
        raise EmbeddedSignupError(
            reason,
            stage="token_exchange",
            meta_error_code=error_code,
        ) from exc

    logger.info(
        "Embedded signup token exchange succeeded: org=%s attempt=%s token_received=True",
        organization.id,
        getattr(attempt, "id", None),
    )
    _update_attempt(
        attempt,
        status="token_exchanged",
        stage="token_exchange",
        token_received=True,
        meta_error_code="",
        error_message="",
    )

    # Step 2: verify the selected phone-number id against that token and collect
    # the human-facing number/name so the user does not have to type them.
    try:
        phone_details = whatsapp_provider.get_phone_number_details(
            phone_number_id=phone_number_id,
            access_token=access_token,
        )
    except WhatsAppAPIError as exc:
        error_code, reason = _meta_error_details(exc)
        raise EmbeddedSignupError(
            reason,
            stage="phone_lookup",
            meta_error_code=error_code,
        ) from exc

    display_phone_number = phone_details.get("display_phone_number", "") or ""
    business_name = phone_details.get("verified_name", "") or ""

    logger.info(
        "Embedded signup phone verified: org=%s attempt=%s waba_id=%s phone_number_id=%s display_phone=%s verified_name=%s",
        organization.id,
        getattr(attempt, "id", None),
        waba_id,
        phone_number_id,
        display_phone_number,
        business_name,
    )
    _update_attempt(
        attempt,
        status="phone_verified",
        stage="phone_lookup",
        waba_id=waba_id,
        phone_number_id=phone_number_id,
        display_phone_number=display_phone_number,
        business_name=business_name,
    )

    # Step 3: persist first. Legacy data can contain duplicate inactive rows for
    # the same phone_number_id, so do not use update_or_create/get here (either
    # can raise MultipleObjectsReturned). Reuse the active/latest canonical row
    # when present; otherwise create one.
    try:
        with transaction.atomic():
            account = (
                WhatsAppAccount.objects.select_for_update()
                .filter(
                    organization=organization,
                    phone_number_id=phone_number_id,
                )
                .order_by("-is_active", "-updated_at", "-connected_at")
                .first()
            )

            if account is None:
                account = WhatsAppAccount.objects.create(
                    organization=organization,
                    connection_type=WhatsAppAccount.ConnectionType.API,
                    waba_id=waba_id,
                    phone_number_id=phone_number_id,
                    display_phone_number=display_phone_number,
                    business_name=business_name,
                    access_token=access_token,
                    status=WhatsAppAccount.Status.CONNECTED,
                    is_active=True,
                )
            else:
                account.connection_type = WhatsAppAccount.ConnectionType.API
                account.waba_id = waba_id
                account.display_phone_number = display_phone_number
                account.business_name = business_name
                account.access_token = access_token
                account.status = WhatsAppAccount.Status.CONNECTED
                account.is_active = True
                account.save(
                    update_fields=[
                        "connection_type",
                        "waba_id",
                        "display_phone_number",
                        "business_name",
                        "access_token",
                        "status",
                        "is_active",
                        "updated_at",
                    ]
                )
    except Exception as exc:
        logger.exception(
            "Embedded signup could not persist WhatsAppAccount: org=%s attempt=%s phone_number_id=%s",
            organization.id,
            getattr(attempt, "id", None),
            phone_number_id,
        )
        raise EmbeddedSignupError(
            "SHVYA received the Meta connection but could not save the WhatsApp account.",
            stage="account_save",
        ) from exc

    logger.info(
        "Embedded signup account saved: org=%s attempt=%s account_id=%s waba_id=%s phone_number_id=%s status=%s active=%s",
        organization.id,
        getattr(attempt, "id", None),
        account.id,
        account.waba_id,
        account.phone_number_id,
        account.status,
        account.is_active,
    )
    _update_attempt(
        attempt,
        account=account,
        stage="account_saved",
        waba_id=account.waba_id,
        phone_number_id=account.phone_number_id,
        display_phone_number=account.display_phone_number,
        business_name=account.business_name,
    )

    # Step 4: webhook subscription is important for inbound messages/statuses,
    # but a failure here must not erase or hide the valid connected account.
    warning = ""
    try:
        whatsapp_provider.subscribe_app_to_waba(
            waba_id=waba_id,
            access_token=access_token,
        )
    except WhatsAppAPIError as exc:
        error_code, reason = _meta_error_details(exc)
        logger.warning(
            "Embedded signup connected account %s but WABA subscription failed: code=%s reason=%s",
            account.id,
            error_code,
            reason,
        )
        warning = (
            "WhatsApp connected successfully, but message notifications could not "
            f"be subscribed yet: {reason}. You can retry the refresh action from "
            "Connected Numbers after fixing the Meta permission/setup."
        )
        _update_attempt(
            attempt,
            status="connected",
            stage="webhook_subscription_warning",
            webhook_subscribed=False,
            meta_error_code=error_code,
            warning_message=warning,
            completed_at=timezone.now(),
        )
    else:
        logger.info(
            "Embedded signup WABA subscription succeeded: account_id=%s waba_id=%s",
            account.id,
            waba_id,
        )
        _update_attempt(
            attempt,
            status="connected",
            stage="connected",
            webhook_subscribed=True,
            meta_error_code="",
            warning_message="",
            completed_at=timezone.now(),
        )

    return account, warning
