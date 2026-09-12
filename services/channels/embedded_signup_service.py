"""Resilient Meta embedded-signup completion for WhatsApp accounts.

The browser handoff and the Meta Graph authorization are deliberately treated
as separate signals. Meta can return the OAuth code before (or without) the
WA_EMBEDDED_SIGNUP FINISH postMessage, so SHVYA can recover missing WABA and
phone IDs from the exchanged business token instead of failing the connection.

Authorization codes and raw access-token values are deliberately never stored
or logged.
"""

import json
import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.channels.models import WhatsAppAccount
from apps.channels.providers import whatsapp as whatsapp_provider
from apps.channels.providers import whatsapp_embedded as embedded_provider
from apps.channels.providers.whatsapp import WhatsAppAPIError

logger = logging.getLogger(__name__)

_REQUIRED_WHATSAPP_SCOPES = {
    "whatsapp_business_management",
    "whatsapp_business_messaging",
}


class EmbeddedSignupError(Exception):
    """Raised when Meta authorization/number lookup cannot be completed."""

    def __init__(self, message, *, stage="", meta_error_code=""):
        super().__init__(message)
        self.stage = stage
        self.meta_error_code = str(meta_error_code or "")


class EmbeddedSignupPhoneSelectionRequired(EmbeddedSignupError):
    """Raised when authorization is valid but Meta exposes multiple phones.

    ``choices`` contains only safe Meta asset metadata. The access token is
    attached in-memory by ``complete_embedded_signup`` immediately before the
    exception leaves the service; callers must never log or render it.
    """

    def __init__(self, choices):
        super().__init__(
            "Choose which authorized WhatsApp phone number SHVYA should connect.",
            stage="phone_selection_required",
        )
        self.choices = choices
        self._access_token = ""

    @property
    def access_token(self):
        return self._access_token


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
    if attempt is None:
        return
    for field, value in changes.items():
        setattr(attempt, field, value)
    attempt.save()


def _unique_ids(values):
    seen = set()
    result = []
    for value in values:
        value = str(value or "").strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _debug_token_waba_ids(debug_data):
    target_ids = []
    for item in debug_data.get("granular_scopes") or []:
        if not isinstance(item, dict):
            continue
        if item.get("scope") not in _REQUIRED_WHATSAPP_SCOPES:
            continue
        target_ids.extend(item.get("target_ids") or [])
    return _unique_ids(target_ids)


def _debug_token_scopes(debug_data):
    scopes = set(debug_data.get("scopes") or [])
    for item in debug_data.get("granular_scopes") or []:
        if isinstance(item, dict) and item.get("scope"):
            scopes.add(item["scope"])
    return scopes


def _provider_error(exc, *, stage="asset_discovery"):
    error_code, reason = _meta_error_details(exc)
    return EmbeddedSignupError(
        reason,
        stage=stage,
        meta_error_code=error_code,
    )


def _list_phones_for_waba(*, waba_id, access_token):
    try:
        phones = embedded_provider.list_waba_phone_numbers(
            waba_id=waba_id,
            access_token=access_token,
        )
    except WhatsAppAPIError as exc:
        raise _provider_error(exc) from exc
    return [item for item in phones if isinstance(item, dict) and item.get("id")]


def _phone_ids_for_waba(*, waba_id, access_token):
    phones = _list_phones_for_waba(
        waba_id=waba_id,
        access_token=access_token,
    )
    return _unique_ids(item.get("id") for item in phones)


def _safe_phone_choice(*, waba_id, phone):
    return {
        "waba_id": str(waba_id),
        "phone_number_id": str(phone.get("id") or ""),
        "display_phone_number": str(phone.get("display_phone_number") or ""),
        "verified_name": str(phone.get("verified_name") or ""),
    }


def _resolve_signup_assets(*, access_token, waba_id="", phone_number_id=""):
    """Recover missing Embedded Signup asset IDs from the authorized token.

    The normal browser FINISH event still supplies both IDs. This recovery path
    exists for Meta/browser handoff races where OAuth succeeds but the FINISH
    postMessage is delayed or absent. If Meta exposes multiple authorized phone
    numbers and does not identify the selected one, the caller receives the safe
    choices so SHVYA can ask the admin directly instead of making them restart
    Meta signup or guessing a number.
    """
    waba_id = str(waba_id or "").strip()
    phone_number_id = str(phone_number_id or "").strip()
    if waba_id and phone_number_id:
        return waba_id, phone_number_id

    candidate_wabas = [waba_id] if waba_id else []

    if not waba_id:
        try:
            debug_data = embedded_provider.debug_access_token(
                app_id=settings.META_APP_ID,
                app_secret=settings.META_APP_SECRET,
                access_token=access_token,
            )
        except WhatsAppAPIError as exc:
            raise _provider_error(exc) from exc

        granted_scopes = _debug_token_scopes(debug_data)
        missing_scopes = _REQUIRED_WHATSAPP_SCOPES - granted_scopes
        if missing_scopes:
            raise EmbeddedSignupError(
                "Meta authorized Facebook Login for Business, but the returned "
                "business token is missing required WhatsApp permissions: "
                + ", ".join(sorted(missing_scopes))
                + ". Verify that the production Configuration ID used by Connect API "
                "is the WhatsApp Embedded Signup configuration and grants both "
                "WhatsApp permissions.",
                stage="asset_discovery",
            )

        candidate_wabas = _debug_token_waba_ids(debug_data)
        if not candidate_wabas and debug_data.get("user_id"):
            try:
                assigned = embedded_provider.list_assigned_wabas(
                    user_id=debug_data["user_id"],
                    access_token=access_token,
                )
            except WhatsAppAPIError as exc:
                raise _provider_error(exc) from exc
            candidate_wabas = _unique_ids(
                item.get("id") for item in assigned if isinstance(item, dict)
            )

        if not candidate_wabas:
            raise EmbeddedSignupError(
                "Meta returned an OAuth token with the required WhatsApp permissions, "
                "but no WhatsApp Business Account was visible to that token. Please "
                "complete Embedded Signup again and select a WhatsApp Business Account.",
                stage="asset_discovery",
            )

    if phone_number_id:
        if len(candidate_wabas) == 1:
            return candidate_wabas[0], phone_number_id

        matching_wabas = []
        for candidate in candidate_wabas:
            if phone_number_id in _phone_ids_for_waba(
                waba_id=candidate,
                access_token=access_token,
            ):
                matching_wabas.append(candidate)

        if len(matching_wabas) == 1:
            return matching_wabas[0], phone_number_id
        if not matching_wabas:
            raise EmbeddedSignupError(
                "Meta returned the selected Phone Number ID, but SHVYA could not match "
                "it to a WhatsApp Business Account visible to the authorized token.",
                stage="asset_discovery",
            )
        raise EmbeddedSignupError(
            "Meta returned an ambiguous WhatsApp Business Account selection. Please "
            "run Connect API again and select the intended account.",
            stage="asset_discovery",
        )

    discovered_choices = []
    seen_pairs = set()
    for candidate in candidate_wabas:
        for phone in _list_phones_for_waba(
            waba_id=candidate,
            access_token=access_token,
        ):
            choice = _safe_phone_choice(waba_id=candidate, phone=phone)
            pair = (choice["waba_id"], choice["phone_number_id"])
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            discovered_choices.append(choice)

    if len(discovered_choices) == 1:
        choice = discovered_choices[0]
        return choice["waba_id"], choice["phone_number_id"]
    if not discovered_choices:
        raise EmbeddedSignupError(
            "Meta authorized the WhatsApp Business Account, but no phone number was "
            "visible to the returned token. Finish adding/verifying a phone number in "
            "Meta Embedded Signup and try again.",
            stage="asset_discovery",
        )
    raise EmbeddedSignupPhoneSelectionRequired(discovered_choices)


def complete_embedded_signup(
    *,
    organization,
    code,
    waba_id="",
    phone_number_id="",
    attempt=None,
    redirect_uri="",
    access_token="",
):
    """Authorize Meta, resolve assets, persist the number, then subscribe webhooks.

    ``access_token`` is used only when resuming a short-lived server-side phone
    selection created after a successful OAuth exchange. Normal callers should
    pass the authorization ``code`` and let this function exchange it.
    """
    if not settings.META_APP_ID or not settings.META_APP_SECRET:
        raise EmbeddedSignupError(
            "Embedded signup is not configured on this server yet.",
            stage="server_configuration",
        )

    access_token = str(access_token or "").strip()
    if not access_token:
        if not code:
            raise EmbeddedSignupError(
                "Meta authorization is no longer available. Please start Connect API again.",
                stage="token_exchange",
            )
        try:
            access_token = embedded_provider.exchange_code_for_access_token(
                app_id=settings.META_APP_ID,
                app_secret=settings.META_APP_SECRET,
                code=code,
                redirect_uri=redirect_uri,
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

    try:
        waba_id, phone_number_id = _resolve_signup_assets(
            access_token=access_token,
            waba_id=waba_id,
            phone_number_id=phone_number_id,
        )
    except EmbeddedSignupPhoneSelectionRequired as exc:
        # Keep the credential only in memory here. The direct OAuth view encrypts
        # it before placing the short-lived continuation in the user's session.
        exc._access_token = access_token
        raise

    logger.info(
        "Embedded signup assets resolved: org=%s attempt=%s waba_id=%s phone_number_id=%s",
        organization.id,
        getattr(attempt, "id", None),
        waba_id,
        phone_number_id,
    )
    _update_attempt(
        attempt,
        stage="asset_discovery",
        waba_id=waba_id,
        phone_number_id=phone_number_id,
    )

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

            WhatsAppAccount.objects.filter(
                organization=organization,
                phone_number_id=phone_number_id,
            ).exclude(id=account.id).update(
                status=WhatsAppAccount.Status.DISCONNECTED,
                is_active=False,
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
