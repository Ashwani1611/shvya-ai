"""Direct Meta OAuth for WhatsApp Business App Coexistence.

The JavaScript SDK can currently enter Meta's FedCM path and return from
``FB.login`` without the exchangeable Login for Business authorization code.
Coexistence needs that code server-side, so this module starts the OAuth dialog
as a normal first-party redirect instead.  It intentionally reuses SHVYA's
existing Connect API direct-return URL so production Meta configuration does not
need a second callback URI.
"""

from __future__ import annotations

import json
import logging
import secrets
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from apps.crm.decorators import crm_login_required
from services.channels.embedded_signup_service import EmbeddedSignupError
from services.channels.whatsapp_coexistence_service import (
    COEXISTENCE_FEATURE_TYPE,
    complete_coexistence_signup,
)

from . import connection_ui, embedded_oauth_ui, views_flat
from .connection_attempts import WhatsAppConnectionAttempt

logger = logging.getLogger(__name__)

_META_OAUTH_DIALOG = "https://www.facebook.com/v25.0/dialog/oauth"
_SESSION_KEY = "whatsapp_coexistence_direct_oauth"
_MAX_AGE_SECONDS = 15 * 60


def _build_coexistence_oauth_url(*, redirect_uri: str, state: str) -> str:
    """Build Login for Business without a ``scope`` or JS/FedCM dependency."""
    params = {
        "client_id": settings.META_APP_ID,
        "redirect_uri": redirect_uri,
        "state": state,
        "config_id": settings.META_WA_EMBEDDED_SIGNUP_CONFIG_ID,
        "response_type": "code",
        "override_default_response_type": "true",
        "auth_type": "rerequest",
        "extras": json.dumps(
            {
                "setup": {},
                "featureType": COEXISTENCE_FEATURE_TYPE,
                "sessionInfoVersion": "3",
            },
            separators=(",", ":"),
        ),
    }
    return f"{_META_OAUTH_DIALOG}?{urlencode(params)}"


def _attempt(request, attempt_id):
    if not attempt_id:
        return None
    return WhatsAppConnectionAttempt.objects.filter(
        id=attempt_id,
        organization=request.crm_user.organization,
    ).first()


def _redirect_to_coexistence(message: str | None = None):
    if message:
        # Caller adds the message because this helper does not receive request.
        pass
    return redirect("whatsapp-connect-coexistence")


@crm_login_required
@require_GET
def whatsapp_coexistence_direct_start_view(request):
    """Start Coexistence through the classic OAuth dialog, bypassing FB.login."""
    user = request.crm_user
    if not views_flat._admin_required(user):
        messages.error(request, "Only organization admins can connect WhatsApp.")
        return redirect("whatsapp-connect-coexistence")

    if not settings.META_APP_ID or not settings.META_WA_EMBEDDED_SIGNUP_CONFIG_ID:
        messages.error(
            request,
            "Meta Coexistence is not configured on this server yet.",
        )
        return redirect("whatsapp-connect-coexistence")

    attempt = WhatsAppConnectionAttempt.objects.create(
        organization=user.organization,
        created_by=user,
        method=WhatsAppConnectionAttempt.Method.EMBEDDED,
        status=WhatsAppConnectionAttempt.Status.STARTED,
        stage="coexistence_direct_oauth_started",
    )
    state = secrets.token_urlsafe(32)
    # Reuse the already deployed/allow-listed direct OAuth callback URL.  The
    # route dispatches back here only while this short-lived session marker is
    # present; ordinary Connect API callbacks continue to the existing view.
    redirect_uri = request.build_absolute_uri(
        reverse("whatsapp-embedded-signup-direct-return")
    )
    request.session[_SESSION_KEY] = {
        "organization_id": str(user.organization_id),
        "attempt_id": str(attempt.id),
        "state": state,
        "redirect_uri": redirect_uri,
        "issued_at": int(timezone.now().timestamp()),
    }
    request.session.modified = True

    logger.info(
        "Direct Coexistence OAuth started: org=%s attempt=%s redirect_host=%s",
        user.organization_id,
        attempt.id,
        request.get_host(),
    )
    return redirect(
        _build_coexistence_oauth_url(
            redirect_uri=redirect_uri,
            state=state,
        )
    )


def whatsapp_embedded_signup_direct_return_dispatch_view(request):
    """Route the shared callback to Coexistence only for its own session."""
    if isinstance(request.session.get(_SESSION_KEY), dict):
        return whatsapp_coexistence_direct_return_view(request)
    return embedded_oauth_ui.whatsapp_embedded_signup_direct_return_view(request)


@crm_login_required
@require_http_methods(["GET"])
def whatsapp_coexistence_direct_return_view(request):
    """Validate direct OAuth state and finish Coexistence server-side."""
    user = request.crm_user
    if not views_flat._admin_required(user):
        messages.error(request, "Only organization admins can connect WhatsApp.")
        return redirect("whatsapp-connect-coexistence")

    payload = request.session.pop(_SESSION_KEY, None)
    request.session.modified = True
    if not isinstance(payload, dict):
        messages.error(
            request,
            "Your Meta Coexistence authorization expired. Please start again.",
        )
        return redirect("whatsapp-connect-coexistence")

    attempt = _attempt(request, payload.get("attempt_id"))
    expected_state = str(payload.get("state") or "")
    returned_state = str(request.GET.get("state") or "")
    redirect_uri = str(payload.get("redirect_uri") or "")

    try:
        issued_at = int(payload.get("issued_at") or 0)
    except (TypeError, ValueError):
        issued_at = 0
    age = int(timezone.now().timestamp()) - issued_at
    state_valid = bool(
        expected_state
        and returned_state
        and secrets.compare_digest(expected_state, returned_state)
        and payload.get("organization_id") == str(user.organization_id)
        and 0 <= age <= _MAX_AGE_SECONDS
    )
    if not state_valid:
        message = (
            "Meta returned an invalid or expired Coexistence authorization. "
            "Please start the connection again."
        )
        if attempt:
            connection_ui._fail_attempt(
                attempt,
                stage="coexistence_direct_oauth_state",
                message=message,
            )
        messages.error(request, message)
        return redirect("whatsapp-connect-coexistence")

    meta_error = str(request.GET.get("error") or "")
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
        messages.error(request, description)
        return redirect("whatsapp-connect-coexistence")

    code = str(request.GET.get("code") or "").strip()
    if not code:
        message = (
            "Meta returned to SHVYA without an authorization code. "
            "Please start WhatsApp Coexistence again."
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

    try:
        account, warning, _sync_results = complete_coexistence_signup(
            organization=user.organization,
            code=code,
            attempt=attempt,
            redirect_uri=redirect_uri,
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
            "Direct Coexistence signup failed: org=%s attempt=%s stage=%s code=%s reason=%s",
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
            "Unexpected direct Coexistence signup failure: org=%s attempt=%s",
            user.organization_id,
            getattr(attempt, "id", None),
        )
        messages.error(request, message)
        return redirect("whatsapp-connect-coexistence")

    if warning:
        messages.warning(request, warning)
    else:
        messages.success(
            request,
            "WhatsApp Business App Coexistence connected successfully.",
        )

    logger.info(
        "Direct Coexistence signup connected: org=%s attempt=%s account=%s",
        user.organization_id,
        getattr(attempt, "id", None),
        account.id,
    )
    return redirect(f"{reverse('whatsapp-accounts')}?connected={account.id}")
