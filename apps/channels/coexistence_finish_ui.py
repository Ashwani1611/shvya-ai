"""Resilient post-Finish OAuth completion for WhatsApp Coexistence.

Meta can complete the Business App partner link successfully and then return to
SHVYA after a long embedded-signup round trip. The original direct OAuth flow
used only a short-lived Django session marker to decide whether the shared
callback belonged to Coexistence. If that marker disappeared, the callback was
misrouted to the normal Connect API flow and the Meta-side link existed without
an active SHVYA account.

This module makes the OAuth ``state`` itself a signed, timestamped routing token.
The existing session marker remains as a backwards-compatible fallback for
already-open signup windows, but a valid signed state is sufficient to route and
complete Coexistence safely.
"""

from __future__ import annotations

import logging
import secrets

from django.conf import settings
from django.contrib import messages
from django.core import signing
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET

from apps.crm.decorators import crm_login_required
from services.channels.embedded_signup_service import EmbeddedSignupError
from services.channels.whatsapp_coexistence_service import complete_coexistence_signup

from . import coexistence_oauth_ui, connection_ui, embedded_oauth_ui, views_flat
from .connection_attempts import WhatsAppConnectionAttempt

logger = logging.getLogger(__name__)

_SIGNED_STATE_SALT = "shvya.whatsapp.coexistence.oauth.v1"
_STATE_MAX_AGE_SECONDS = 15 * 60
_FLOW = "coexistence"


def _decode_signed_state(value):
    value = str(value or "").strip()
    if not value:
        return None
    try:
        payload = signing.loads(
            value,
            salt=_SIGNED_STATE_SALT,
            max_age=_STATE_MAX_AGE_SECONDS,
        )
    except (signing.BadSignature, signing.SignatureExpired):
        return None
    if not isinstance(payload, dict) or payload.get("flow") != _FLOW:
        return None
    return payload


def _build_signed_state(*, organization_id, attempt_id, redirect_uri):
    return signing.dumps(
        {
            "flow": _FLOW,
            "organization_id": str(organization_id),
            "attempt_id": str(attempt_id),
            "redirect_uri": str(redirect_uri),
            "nonce": secrets.token_urlsafe(18),
        },
        salt=_SIGNED_STATE_SALT,
        compress=True,
    )


def _attempt_for_payload(request, payload):
    attempt_id = str((payload or {}).get("attempt_id") or "").strip()
    if not attempt_id:
        return None
    return WhatsAppConnectionAttempt.objects.filter(
        id=attempt_id,
        organization=request.crm_user.organization,
    ).first()


def _session_payload(request):
    payload = request.session.get(coexistence_oauth_ui._SESSION_KEY)
    return payload if isinstance(payload, dict) else None


def _legacy_session_payload_is_valid(request, payload, returned_state):
    if not isinstance(payload, dict):
        return False
    expected_state = str(payload.get("state") or "")
    if not expected_state or not returned_state:
        return False
    try:
        issued_at = int(payload.get("issued_at") or 0)
    except (TypeError, ValueError):
        return False
    age = int(timezone.now().timestamp()) - issued_at
    return bool(
        secrets.compare_digest(expected_state, returned_state)
        and payload.get("organization_id") == str(request.crm_user.organization_id)
        and 0 <= age <= _STATE_MAX_AGE_SECONDS
    )


def _completion_payload(request):
    """Return a validated signed payload or a valid legacy session payload."""
    returned_state = str(request.GET.get("state") or "").strip()
    signed_payload = _decode_signed_state(returned_state)
    if signed_payload is not None:
        if signed_payload.get("organization_id") != str(request.crm_user.organization_id):
            return None
        return signed_payload

    legacy = _session_payload(request)
    if _legacy_session_payload_is_valid(request, legacy, returned_state):
        return legacy
    return None


@crm_login_required
@require_GET
def whatsapp_coexistence_direct_start_view(request):
    """Start direct Login for Business with a self-validating Coexistence state."""
    user = request.crm_user
    if not views_flat._admin_required(user):
        messages.error(request, "Only organization admins can connect WhatsApp.")
        return redirect("whatsapp-connect-coexistence")

    if not settings.META_APP_ID or not settings.META_WA_EMBEDDED_SIGNUP_CONFIG_ID:
        messages.error(request, "Meta Coexistence is not configured on this server yet.")
        return redirect("whatsapp-connect-coexistence")

    attempt = WhatsAppConnectionAttempt.objects.create(
        organization=user.organization,
        created_by=user,
        method=WhatsAppConnectionAttempt.Method.EMBEDDED,
        status=WhatsAppConnectionAttempt.Status.STARTED,
        stage="coexistence_direct_oauth_started",
    )
    redirect_uri = request.build_absolute_uri(
        reverse("whatsapp-embedded-signup-direct-return")
    )
    state = _build_signed_state(
        organization_id=user.organization_id,
        attempt_id=attempt.id,
        redirect_uri=redirect_uri,
    )

    # Keep the old marker during the rollout so already-tested behavior remains
    # compatible, but completion no longer depends on this marker being present.
    request.session[coexistence_oauth_ui._SESSION_KEY] = {
        "organization_id": str(user.organization_id),
        "attempt_id": str(attempt.id),
        "state": state,
        "redirect_uri": redirect_uri,
        "issued_at": int(timezone.now().timestamp()),
    }
    request.session.modified = True

    logger.info(
        "Resilient Coexistence OAuth started: org=%s attempt=%s redirect_host=%s",
        user.organization_id,
        attempt.id,
        request.get_host(),
    )
    return redirect(
        coexistence_oauth_ui._build_coexistence_oauth_url(
            redirect_uri=redirect_uri,
            state=state,
        )
    )


def whatsapp_embedded_signup_direct_return_dispatch_view(request):
    """Dispatch shared OAuth returns using signed state before session fallback."""
    if _decode_signed_state(request.GET.get("state")) is not None:
        return whatsapp_coexistence_direct_return_view(request)
    if isinstance(request.session.get(coexistence_oauth_ui._SESSION_KEY), dict):
        return whatsapp_coexistence_direct_return_view(request)
    return embedded_oauth_ui.whatsapp_embedded_signup_direct_return_view(request)


@crm_login_required
@require_GET
def whatsapp_coexistence_direct_return_view(request):
    """Finish Coexistence, mark the account active, then open the API inbox."""
    user = request.crm_user
    if not views_flat._admin_required(user):
        messages.error(request, "Only organization admins can connect WhatsApp.")
        return redirect("whatsapp-connect-coexistence")

    payload = _completion_payload(request)
    # The marker is one-time state. Consume it after validation regardless of
    # whether the signed token or the legacy session path supplied the payload.
    request.session.pop(coexistence_oauth_ui._SESSION_KEY, None)
    request.session.modified = True

    if payload is None:
        messages.error(
            request,
            "Meta returned an invalid or expired Coexistence authorization. Please start again.",
        )
        return redirect("whatsapp-connect-coexistence")

    attempt = _attempt_for_payload(request, payload)
    redirect_uri = str(payload.get("redirect_uri") or "").strip()

    meta_error = str(request.GET.get("error") or "").strip()
    if meta_error:
        description = (
            request.GET.get("error_description")
            or request.GET.get("error_message")
            or "Meta did not complete WhatsApp Coexistence authorization."
        )
        cancelled = meta_error == "access_denied"
        if attempt:
            connection_ui._set_attempt(
                attempt,
                status=(
                    WhatsAppConnectionAttempt.Status.CANCELLED
                    if cancelled
                    else WhatsAppConnectionAttempt.Status.FAILED
                ),
                stage=(
                    "coexistence_direct_oauth_cancelled"
                    if cancelled
                    else "coexistence_direct_oauth_error"
                ),
                meta_error_code=str(request.GET.get("error_code") or meta_error)[:64],
                error_message=str(description)[:4000],
                completed_at=timezone.now(),
            )
        messages.error(request, str(description))
        return redirect("whatsapp-connect-coexistence")

    code = str(request.GET.get("code") or "").strip()
    if not code:
        message = (
            "Meta finished linking the account but returned to SHVYA without an "
            "authorization code. Please run Coexistence setup once more."
        )
        if attempt:
            connection_ui._fail_attempt(
                attempt,
                stage="coexistence_direct_oauth_code_missing",
                message=message,
            )
        messages.error(request, message)
        return redirect("whatsapp-connect-coexistence")

    if attempt:
        connection_ui._set_attempt(
            attempt,
            status=WhatsAppConnectionAttempt.Status.CODE_RECEIVED,
            stage="coexistence_direct_oauth_code_received",
            code_received=True,
            completed_at=None,
            error_message="",
            meta_error_code="",
        )

    # Prefer any explicit Meta callback hints, then safe attempt telemetry. The
    # service still recovers both IDs from the business token when Meta omits them.
    waba_id = str(
        request.GET.get("waba_id")
        or request.GET.get("wabaId")
        or getattr(attempt, "waba_id", "")
        or ""
    ).strip()
    phone_number_id = str(
        request.GET.get("phone_number_id")
        or request.GET.get("phoneNumberId")
        or getattr(attempt, "phone_number_id", "")
        or ""
    ).strip()

    try:
        from apps.channels.providers import whatsapp_embedded as embedded_provider

        with embedded_provider.oauth_redirect_uri(redirect_uri):
            account, warning, sync_results = complete_coexistence_signup(
                organization=user.organization,
                code=code,
                waba_id=waba_id,
                phone_number_id=phone_number_id,
                attempt=attempt,
            )
    except EmbeddedSignupError as exc:
        if attempt:
            connection_ui._fail_attempt(
                attempt,
                stage=exc.stage or "coexistence_direct_signup",
                message=str(exc),
                meta_error_code=exc.meta_error_code,
            )
        logger.warning(
            "Resilient Coexistence completion failed: org=%s attempt=%s stage=%s meta_code=%s reason=%s",
            user.organization_id,
            getattr(attempt, "id", None),
            exc.stage,
            exc.meta_error_code,
            exc,
        )
        messages.error(request, str(exc))
        return redirect("whatsapp-connect-coexistence")
    except Exception:
        message = "Unexpected server error while completing WhatsApp Coexistence setup."
        if attempt:
            connection_ui._fail_attempt(
                attempt,
                stage="coexistence_direct_server_error",
                message=message,
            )
        logger.exception(
            "Unexpected resilient Coexistence completion failure: org=%s attempt=%s",
            user.organization_id,
            getattr(attempt, "id", None),
        )
        messages.error(request, message)
        return redirect("whatsapp-connect-coexistence")

    messages.success(
        request,
        "WhatsApp Coexistence connected successfully. New chats are active in SHVYA.",
    )
    if warning:
        messages.warning(
            request,
            "The account is connected, but part of the initial history/contact sync needs attention: "
            + warning,
        )
    elif sync_results:
        messages.info(
            request,
            "Your WhatsApp Business App chat history and contacts are syncing. "
            "Meta may deliver the imported conversations over the next few minutes.",
        )

    logger.info(
        "Resilient Coexistence connected: org=%s attempt=%s account=%s",
        user.organization_id,
        getattr(attempt, "id", None),
        account.id,
    )
    return redirect(
        f"{reverse('whatsapp-chats')}?connected={account.id}&coexistence=1"
    )
