"""Direct Facebook Login for Business OAuth for WhatsApp Embedded Signup.

Chrome/Meta can currently route the JavaScript SDK through FedCM even when the
page has not opted in. Login for Business configurations are not supported by
that FedCM path, which can close the flow before ``FB.login`` returns a code.

This module bypasses the JS SDK entirely: SHVYA starts the documented OAuth
dialog directly with the same Login for Business ``config_id`` and receives the
authorization code on a first-party callback URL. No ``scope`` parameter is
sent; the v4 configuration owns the WhatsApp permissions.
"""

import logging
import secrets
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET

from apps.crm.decorators import crm_login_required
from services.channels.embedded_signup_service import (
    EmbeddedSignupError,
    complete_embedded_signup,
)

from . import views_flat
from .connection_attempts import WhatsAppConnectionAttempt

logger = logging.getLogger(__name__)

_META_OAUTH_DIALOG = "https://www.facebook.com/v25.0/dialog/oauth"
_SESSION_STATE = "whatsapp_embedded_oauth_state"
_SESSION_ATTEMPT = "whatsapp_embedded_oauth_attempt"
_SESSION_REDIRECT_URI = "whatsapp_embedded_oauth_redirect_uri"


def _set_attempt(attempt, **changes):
    if attempt is None:
        return
    for field, value in changes.items():
        setattr(attempt, field, value)
    attempt.save()


def _fail_attempt(attempt, *, stage, message, meta_error_code="", cancelled=False):
    _set_attempt(
        attempt,
        status=(
            WhatsAppConnectionAttempt.Status.CANCELLED
            if cancelled
            else WhatsAppConnectionAttempt.Status.FAILED
        ),
        stage=stage,
        meta_error_code=str(meta_error_code or "")[:64],
        error_message=(message or "")[:4000],
        completed_at=timezone.now(),
    )


def _attempt_from_session(request):
    attempt_id = request.session.get(_SESSION_ATTEMPT)
    if not attempt_id:
        return None
    return WhatsAppConnectionAttempt.objects.filter(
        id=attempt_id,
        organization=request.crm_user.organization,
    ).first()


def _build_meta_oauth_url(*, app_id, config_id, redirect_uri, state):
    """Build the non-FedCM Login for Business authorization URL."""
    params = {
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "config_id": config_id,
        "response_type": "code",
        "override_default_response_type": "true",
        "auth_type": "rerequest",
    }
    return f"{_META_OAUTH_DIALOG}?{urlencode(params)}"


@crm_login_required
@require_GET
def whatsapp_embedded_signup_direct_start_view(request):
    """Start Embedded Signup without invoking the Facebook JavaScript SDK."""
    user = request.crm_user
    if not views_flat._admin_required(user):
        messages.error(request, "Only organization admins can connect WhatsApp.")
        return redirect("whatsapp-connect-api")

    if not settings.META_APP_ID or not settings.META_WA_EMBEDDED_SIGNUP_CONFIG_ID:
        messages.error(
            request,
            "Meta Embedded Signup is not configured on this server yet.",
        )
        return redirect("whatsapp-connect-api")

    attempt = WhatsAppConnectionAttempt.objects.create(
        organization=user.organization,
        created_by=user,
        method=WhatsAppConnectionAttempt.Method.EMBEDDED,
        status=WhatsAppConnectionAttempt.Status.STARTED,
        stage="direct_oauth_started",
    )
    state = secrets.token_urlsafe(32)
    redirect_uri = request.build_absolute_uri(
        reverse("whatsapp-embedded-signup-direct-return")
    )

    request.session[_SESSION_STATE] = state
    request.session[_SESSION_ATTEMPT] = str(attempt.id)
    request.session[_SESSION_REDIRECT_URI] = redirect_uri
    request.session.modified = True

    logger.info(
        "Direct Embedded Signup OAuth started: org=%s attempt=%s redirect_host=%s",
        user.organization_id,
        attempt.id,
        request.get_host(),
    )

    return redirect(
        _build_meta_oauth_url(
            app_id=settings.META_APP_ID,
            config_id=settings.META_WA_EMBEDDED_SIGNUP_CONFIG_ID,
            redirect_uri=redirect_uri,
            state=state,
        )
    )


@crm_login_required
@require_GET
def whatsapp_embedded_signup_direct_return_view(request):
    """Validate OAuth state, exchange Meta's code, and save the WhatsApp account."""
    user = request.crm_user
    if not views_flat._admin_required(user):
        messages.error(request, "Only organization admins can connect WhatsApp.")
        return redirect("whatsapp-connect-api")

    expected_state = request.session.get(_SESSION_STATE) or ""
    returned_state = request.GET.get("state") or ""
    attempt = _attempt_from_session(request)
    redirect_uri = request.session.get(_SESSION_REDIRECT_URI) or request.build_absolute_uri(
        reverse("whatsapp-embedded-signup-direct-return")
    )

    # Consume the one-time state before doing any token work so a callback URL
    # cannot be replayed after either success or failure.
    request.session.pop(_SESSION_STATE, None)
    request.session.pop(_SESSION_ATTEMPT, None)
    request.session.pop(_SESSION_REDIRECT_URI, None)
    request.session.modified = True

    if not expected_state or not returned_state or not secrets.compare_digest(
        expected_state,
        returned_state,
    ):
        message = (
            "Meta returned an invalid or expired authorization state. "
            "Please start Connect API again."
        )
        _fail_attempt(attempt, stage="direct_oauth_state", message=message)
        messages.error(request, message)
        return redirect("whatsapp-connect-api")

    meta_error = request.GET.get("error") or ""
    if meta_error:
        description = (
            request.GET.get("error_description")
            or request.GET.get("error_message")
            or "Meta did not complete WhatsApp authorization."
        )
        error_code = request.GET.get("error_code") or meta_error
        cancelled = meta_error == "access_denied"
        _fail_attempt(
            attempt,
            stage="direct_oauth_cancelled" if cancelled else "direct_oauth_error",
            message=description,
            meta_error_code=error_code,
            cancelled=cancelled,
        )
        messages.error(request, description)
        return redirect("whatsapp-connect-api")

    code = (request.GET.get("code") or "").strip()
    if not code:
        message = (
            "Meta returned to SHVYA without an OAuth authorization code. "
            "Please start Connect API again."
        )
        _fail_attempt(attempt, stage="direct_oauth_code_missing", message=message)
        messages.error(request, message)
        return redirect("whatsapp-connect-api")

    _set_attempt(
        attempt,
        status=WhatsAppConnectionAttempt.Status.CODE_RECEIVED,
        stage="direct_oauth_code_received",
        code_received=True,
        completed_at=None,
        error_message="",
        meta_error_code="",
    )

    try:
        account, warning = complete_embedded_signup(
            organization=user.organization,
            code=code,
            attempt=attempt,
            redirect_uri=redirect_uri,
        )
    except EmbeddedSignupError as exc:
        _fail_attempt(
            attempt,
            stage=exc.stage or "direct_embedded_signup",
            message=str(exc),
            meta_error_code=exc.meta_error_code,
        )
        logger.warning(
            "Direct Embedded Signup failed: org=%s attempt=%s stage=%s meta_code=%s reason=%s",
            user.organization_id,
            getattr(attempt, "id", None),
            exc.stage,
            exc.meta_error_code,
            exc,
        )
        messages.error(request, str(exc))
        return redirect("whatsapp-connect-api")
    except Exception:
        message = "Unexpected server error while completing WhatsApp connection."
        _fail_attempt(attempt, stage="server_error", message=message)
        logger.exception(
            "Unexpected direct Embedded Signup failure: org=%s attempt=%s",
            user.organization_id,
            getattr(attempt, "id", None),
        )
        messages.error(request, message)
        return redirect("whatsapp-connect-api")

    if warning:
        messages.warning(request, warning)
    else:
        messages.success(request, "WhatsApp Business API connected successfully.")

    logger.info(
        "Direct Embedded Signup connected: org=%s attempt=%s account=%s",
        user.organization_id,
        getattr(attempt, "id", None),
        account.id,
    )
    return redirect(f"{reverse('whatsapp-accounts')}?connected={account.id}")
