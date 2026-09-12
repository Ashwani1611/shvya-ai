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

from cryptography.fernet import InvalidToken
from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from apps.channels.models import _fernet
from apps.crm.decorators import crm_login_required
from services.channels.embedded_signup_service import (
    EmbeddedSignupError,
    EmbeddedSignupPhoneSelectionRequired,
    complete_embedded_signup,
)

from . import views_flat
from .connection_attempts import WhatsAppConnectionAttempt

logger = logging.getLogger(__name__)

_META_OAUTH_DIALOG = "https://www.facebook.com/v25.0/dialog/oauth"
_SESSION_STATE = "whatsapp_embedded_oauth_state"
_SESSION_ATTEMPT = "whatsapp_embedded_oauth_attempt"
_SESSION_REDIRECT_URI = "whatsapp_embedded_oauth_redirect_uri"
_SESSION_PHONE_SELECTION = "whatsapp_embedded_phone_selection"
_PHONE_SELECTION_MAX_AGE_SECONDS = 10 * 60


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


def _attempt_for_id(request, attempt_id):
    if not attempt_id:
        return None
    return WhatsAppConnectionAttempt.objects.filter(
        id=attempt_id,
        organization=request.crm_user.organization,
    ).first()


def _attempt_from_session(request):
    return _attempt_for_id(request, request.session.get(_SESSION_ATTEMPT))


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


def _store_pending_phone_selection(request, *, attempt, selection_error):
    """Store a short-lived, encrypted continuation for an explicit phone pick."""
    token = selection_error.access_token
    if not token:
        raise EmbeddedSignupError(
            "Meta authorization could not be resumed. Please start Connect API again.",
            stage="phone_selection_state",
        )

    request.session[_SESSION_PHONE_SELECTION] = {
        "organization_id": str(request.crm_user.organization_id),
        "attempt_id": str(getattr(attempt, "id", "") or ""),
        "issued_at": int(timezone.now().timestamp()),
        "access_token": _fernet().encrypt(token.encode("utf-8")).decode("ascii"),
        "choices": list(selection_error.choices),
    }
    request.session.modified = True


def _load_pending_phone_selection(request):
    payload = request.session.get(_SESSION_PHONE_SELECTION)
    if not isinstance(payload, dict):
        return None
    if payload.get("organization_id") != str(request.crm_user.organization_id):
        return None

    try:
        issued_at = int(payload.get("issued_at") or 0)
    except (TypeError, ValueError):
        return None
    age = int(timezone.now().timestamp()) - issued_at
    if age < 0 or age > _PHONE_SELECTION_MAX_AGE_SECONDS:
        return None

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    if not payload.get("access_token"):
        return None
    return payload


def _clear_pending_phone_selection(request):
    request.session.pop(_SESSION_PHONE_SELECTION, None)
    request.session.modified = True


def _render_phone_selection(request, payload, *, status=200, error_message=""):
    return render(
        request,
        "channels/whatsapp_select_phone.html",
        {
            "phone_choices": payload.get("choices") or [],
            "error_message": error_message,
        },
        status=status,
    )


def _complete_selected_phone(request):
    payload = _load_pending_phone_selection(request)
    if payload is None:
        _clear_pending_phone_selection(request)
        messages.error(
            request,
            "Your WhatsApp phone selection expired. Please start Connect API again.",
        )
        return redirect("whatsapp-connect-api")

    selected_phone_id = (request.POST.get("phone_number_id") or "").strip()
    selected_choice = next(
        (
            choice
            for choice in payload["choices"]
            if isinstance(choice, dict)
            and str(choice.get("phone_number_id") or "") == selected_phone_id
        ),
        None,
    )
    if selected_choice is None:
        return _render_phone_selection(
            request,
            payload,
            status=400,
            error_message="Select one of the authorized WhatsApp phone numbers below.",
        )

    try:
        access_token = (
            _fernet()
            .decrypt(str(payload["access_token"]).encode("ascii"))
            .decode("utf-8")
        )
    except (InvalidToken, UnicodeError, ValueError):
        _clear_pending_phone_selection(request)
        messages.error(
            request,
            "The secure WhatsApp authorization expired. Please start Connect API again.",
        )
        return redirect("whatsapp-connect-api")

    attempt = _attempt_for_id(request, payload.get("attempt_id"))
    _clear_pending_phone_selection(request)
    _set_attempt(
        attempt,
        status=WhatsAppConnectionAttempt.Status.TOKEN_EXCHANGED,
        stage="phone_selection_submitted",
        completed_at=None,
        error_message="",
        meta_error_code="",
    )

    try:
        account, warning = complete_embedded_signup(
            organization=request.crm_user.organization,
            code="",
            waba_id=str(selected_choice.get("waba_id") or ""),
            phone_number_id=selected_phone_id,
            attempt=attempt,
            access_token=access_token,
        )
    except EmbeddedSignupError as exc:
        _fail_attempt(
            attempt,
            stage=exc.stage or "direct_embedded_signup",
            message=str(exc),
            meta_error_code=exc.meta_error_code,
        )
        logger.warning(
            "Direct Embedded Signup phone completion failed: org=%s attempt=%s stage=%s meta_code=%s reason=%s",
            request.crm_user.organization_id,
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
            "Unexpected phone-selection completion failure: org=%s attempt=%s",
            request.crm_user.organization_id,
            getattr(attempt, "id", None),
        )
        messages.error(request, message)
        return redirect("whatsapp-connect-api")

    if warning:
        messages.warning(request, warning)
    else:
        messages.success(request, "WhatsApp Business API connected successfully.")

    logger.info(
        "Direct Embedded Signup connected after phone selection: org=%s attempt=%s account=%s",
        request.crm_user.organization_id,
        getattr(attempt, "id", None),
        account.id,
    )
    return redirect(f"{reverse('whatsapp-accounts')}?connected={account.id}")


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

    # A fresh Meta flow invalidates any unfinished prior phone selection.
    _clear_pending_phone_selection(request)

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
@require_http_methods(["GET", "POST"])
def whatsapp_embedded_signup_direct_return_view(request):
    """Validate OAuth state, then complete or resume Embedded Signup."""
    user = request.crm_user
    if not views_flat._admin_required(user):
        messages.error(request, "Only organization admins can connect WhatsApp.")
        return redirect("whatsapp-connect-api")

    if request.method == "POST":
        return _complete_selected_phone(request)

    if request.GET.get("choose") == "1":
        payload = _load_pending_phone_selection(request)
        if payload is None:
            _clear_pending_phone_selection(request)
            messages.error(
                request,
                "Your WhatsApp phone selection expired. Please start Connect API again.",
            )
            return redirect("whatsapp-connect-api")
        return _render_phone_selection(request, payload)

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
    except EmbeddedSignupPhoneSelectionRequired as exc:
        try:
            _store_pending_phone_selection(
                request,
                attempt=attempt,
                selection_error=exc,
            )
        except EmbeddedSignupError as state_exc:
            _fail_attempt(
                attempt,
                stage=state_exc.stage,
                message=str(state_exc),
            )
            messages.error(request, str(state_exc))
            return redirect("whatsapp-connect-api")

        _set_attempt(
            attempt,
            status=WhatsAppConnectionAttempt.Status.TOKEN_EXCHANGED,
            stage="phone_selection_required",
            completed_at=None,
            error_message="",
            meta_error_code="",
        )
        logger.info(
            "Direct Embedded Signup requires phone selection: org=%s attempt=%s choices=%s",
            user.organization_id,
            getattr(attempt, "id", None),
            len(exc.choices),
        )
        return redirect(
            f"{reverse('whatsapp-embedded-signup-direct-return')}?choose=1"
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
