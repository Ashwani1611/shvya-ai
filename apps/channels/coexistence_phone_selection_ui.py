"""Finish WhatsApp Coexistence when Meta authorizes more than one phone.

Meta's Coexistence Embedded Signup can finish successfully without returning a
``phone_number_id``. When the authorized WABA owns multiple phone numbers,
SHVYA must not guess. This module keeps the exchanged business token encrypted
in the user's session for a few minutes, renders an explicit phone picker, and
then resumes the normal Coexistence completion with the selected number.

The continuation never stores or renders a plaintext access token.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging

from cryptography.fernet import InvalidToken
from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from apps.channels.models import _fernet
from apps.channels.providers import whatsapp_embedded as embedded_provider
from apps.channels.providers.whatsapp import WhatsAppAPIError
from apps.crm.decorators import crm_login_required
from services.channels import whatsapp_coexistence_service as coexistence_service
from services.channels.embedded_signup_service import (
    EmbeddedSignupError,
    EmbeddedSignupPhoneSelectionRequired,
    _meta_error_details,
)

from . import coexistence_finish_ui, coexistence_oauth_ui, connection_ui, embedded_oauth_ui, views_flat
from .connection_attempts import WhatsAppConnectionAttempt

logger = logging.getLogger(__name__)

_SESSION_PHONE_SELECTION = "whatsapp_coexistence_phone_selection"
_PHONE_SELECTION_MAX_AGE_SECONDS = 10 * 60
_RESUME_TOKEN = contextvars.ContextVar("whatsapp_coexistence_resume_token", default="")
_ORIGINAL_EXCHANGE = embedded_provider.exchange_code_for_access_token


def _exchange_with_resume_token(*args, **kwargs):
    token = _RESUME_TOKEN.get()
    if token:
        return token
    return _ORIGINAL_EXCHANGE(*args, **kwargs)


# Install once. ContextVar keeps the override request-local even under threaded
# or async-capable workers, unlike mutating the provider function per request.
if embedded_provider.exchange_code_for_access_token is not _exchange_with_resume_token:
    embedded_provider.exchange_code_for_access_token = _exchange_with_resume_token


@contextlib.contextmanager
def _resume_with_access_token(access_token):
    marker = _RESUME_TOKEN.set(str(access_token or ""))
    try:
        yield
    finally:
        _RESUME_TOKEN.reset(marker)


def _attempt_for_id(request, attempt_id):
    if not attempt_id:
        return None
    return WhatsAppConnectionAttempt.objects.filter(
        id=attempt_id,
        organization=request.crm_user.organization,
    ).first()


def _store_pending_selection(request, *, attempt, access_token, choices, redirect_uri):
    if not access_token:
        raise EmbeddedSignupError(
            "Meta authorization could not be resumed. Please start Coexistence setup again.",
            stage="coexistence_phone_selection_state",
        )
    safe_choices = [choice for choice in choices if isinstance(choice, dict)]
    if not safe_choices:
        raise EmbeddedSignupError(
            "Meta did not return any selectable WhatsApp phone numbers.",
            stage="coexistence_phone_selection_state",
        )

    request.session[_SESSION_PHONE_SELECTION] = {
        "organization_id": str(request.crm_user.organization_id),
        "attempt_id": str(getattr(attempt, "id", "") or ""),
        "issued_at": int(timezone.now().timestamp()),
        "redirect_uri": str(redirect_uri or ""),
        "access_token": _fernet().encrypt(access_token.encode("utf-8")).decode("ascii"),
        "choices": safe_choices,
    }
    request.session.modified = True


def _load_pending_selection(request):
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
    if not payload.get("access_token"):
        return None
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    return payload


def _clear_pending_selection(request):
    request.session.pop(_SESSION_PHONE_SELECTION, None)
    request.session.modified = True


def _render_phone_selection(request, payload, *, status=200, error_message=""):
    return render(
        request,
        "channels/whatsapp_select_coexistence_phone.html",
        {
            "phone_choices": payload.get("choices") or [],
            "error_message": error_message,
        },
        status=status,
    )


def _decrypt_access_token(payload):
    try:
        return (
            _fernet()
            .decrypt(str(payload["access_token"]).encode("ascii"))
            .decode("utf-8")
        )
    except (InvalidToken, UnicodeError, ValueError, KeyError) as exc:
        raise EmbeddedSignupError(
            "The secure Meta authorization expired. Please start Coexistence setup again.",
            stage="coexistence_phone_selection_state",
        ) from exc


def _complete_with_token(*, organization, access_token, waba_id, phone_number_id, attempt):
    # The normal service owns validation, persistence, webhook subscription and
    # history/contact sync. We only bypass a second OAuth-code exchange because
    # Meta authorization codes are one-time-use.
    with _resume_with_access_token(access_token):
        return coexistence_service.complete_coexistence_signup(
            organization=organization,
            code="coexistence-resume",
            waba_id=waba_id,
            phone_number_id=phone_number_id,
            attempt=attempt,
        )


def _finish_success(request, *, account, warning, sync_results):
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
    return redirect(f"{reverse('whatsapp-chats')}?connected={account.id}&coexistence=1")


def whatsapp_embedded_signup_direct_return_dispatch_view(request):
    """Route the shared Meta return without changing normal Connect API behavior."""
    if coexistence_finish_ui._decode_signed_state(request.GET.get("state")) is not None:
        return whatsapp_coexistence_direct_return_view(request)
    if isinstance(request.session.get(coexistence_oauth_ui._SESSION_KEY), dict):
        return whatsapp_coexistence_direct_return_view(request)
    return embedded_oauth_ui.whatsapp_embedded_signup_direct_return_view(request)


@crm_login_required
@require_GET
def whatsapp_coexistence_direct_return_view(request):
    """Exchange Meta's code once, resolve assets, or continue through a picker."""
    user = request.crm_user
    if not views_flat._admin_required(user):
        messages.error(request, "Only organization admins can connect WhatsApp.")
        return redirect("whatsapp-connect-coexistence")

    payload = coexistence_finish_ui._completion_payload(request)
    request.session.pop(coexistence_oauth_ui._SESSION_KEY, None)
    request.session.modified = True

    if payload is None:
        messages.error(
            request,
            "Meta returned an invalid or expired Coexistence authorization. Please start again.",
        )
        return redirect("whatsapp-connect-coexistence")

    attempt = coexistence_finish_ui._attempt_for_payload(request, payload)
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
        with embedded_provider.oauth_redirect_uri(redirect_uri):
            access_token = _ORIGINAL_EXCHANGE(
                app_id=coexistence_service.settings.META_APP_ID,
                app_secret=coexistence_service.settings.META_APP_SECRET,
                code=code,
                redirect_uri="",
            )

        try:
            resolved_waba_id, resolved_phone_id = coexistence_service._resolve_signup_assets(
                access_token=access_token,
                waba_id=waba_id,
                phone_number_id=phone_number_id,
            )
        except EmbeddedSignupPhoneSelectionRequired as selection:
            _store_pending_selection(
                request,
                attempt=attempt,
                access_token=access_token,
                choices=selection.choices,
                redirect_uri=redirect_uri,
            )
            if attempt:
                connection_ui._set_attempt(
                    attempt,
                    status=WhatsAppConnectionAttempt.Status.TOKEN_EXCHANGED,
                    stage="coexistence_phone_selection_required",
                    token_received=True,
                    completed_at=None,
                    error_message="",
                    meta_error_code="",
                )
            logger.info(
                "Coexistence requires explicit phone selection: org=%s attempt=%s choices=%s",
                user.organization_id,
                getattr(attempt, "id", None),
                len(selection.choices or []),
            )
            return redirect("whatsapp-coexistence-select-phone")

        account, warning, sync_results = _complete_with_token(
            organization=user.organization,
            access_token=access_token,
            waba_id=resolved_waba_id,
            phone_number_id=resolved_phone_id,
            attempt=attempt,
        )
    except WhatsAppAPIError as exc:
        code_value, reason = _meta_error_details(exc)
        failure = EmbeddedSignupError(
            reason,
            stage="token_exchange",
            meta_error_code=code_value,
        )
        if attempt:
            connection_ui._fail_attempt(
                attempt,
                stage=failure.stage,
                message=str(failure),
                meta_error_code=failure.meta_error_code,
            )
        messages.error(request, str(failure))
        return redirect("whatsapp-connect-coexistence")
    except EmbeddedSignupError as exc:
        if attempt:
            connection_ui._fail_attempt(
                attempt,
                stage=exc.stage or "coexistence_direct_signup",
                message=str(exc),
                meta_error_code=exc.meta_error_code,
            )
        logger.warning(
            "Coexistence completion failed: org=%s attempt=%s stage=%s meta_code=%s reason=%s",
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
            "Unexpected Coexistence completion failure: org=%s attempt=%s",
            user.organization_id,
            getattr(attempt, "id", None),
        )
        messages.error(request, message)
        return redirect("whatsapp-connect-coexistence")

    logger.info(
        "Coexistence connected: org=%s attempt=%s account=%s",
        user.organization_id,
        getattr(attempt, "id", None),
        account.id,
    )
    return _finish_success(
        request,
        account=account,
        warning=warning,
        sync_results=sync_results,
    )


@crm_login_required
@require_http_methods(["GET", "POST"])
def whatsapp_coexistence_select_phone_view(request):
    """Render/submit the short-lived continuation after an ambiguous Meta grant."""
    user = request.crm_user
    if not views_flat._admin_required(user):
        messages.error(request, "Only organization admins can connect WhatsApp.")
        return redirect("whatsapp-connect-coexistence")

    payload = _load_pending_selection(request)
    if payload is None:
        _clear_pending_selection(request)
        messages.error(
            request,
            "Your WhatsApp Coexistence phone selection expired. Please start setup again.",
        )
        return redirect("whatsapp-connect-coexistence")

    if request.method == "GET":
        return _render_phone_selection(request, payload)

    selected_phone_id = str(request.POST.get("phone_number_id") or "").strip()
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
            error_message="Select one of the authorized WhatsApp Business App numbers below.",
        )

    attempt = _attempt_for_id(request, payload.get("attempt_id"))
    try:
        access_token = _decrypt_access_token(payload)
        account, warning, sync_results = _complete_with_token(
            organization=user.organization,
            access_token=access_token,
            waba_id=str(selected_choice.get("waba_id") or ""),
            phone_number_id=selected_phone_id,
            attempt=attempt,
        )
    except EmbeddedSignupError as exc:
        _clear_pending_selection(request)
        if attempt:
            connection_ui._fail_attempt(
                attempt,
                stage=exc.stage or "coexistence_phone_selection",
                message=str(exc),
                meta_error_code=exc.meta_error_code,
            )
        messages.error(request, str(exc))
        return redirect("whatsapp-connect-coexistence")
    except Exception:
        _clear_pending_selection(request)
        message = "Unexpected server error while connecting the selected WhatsApp number."
        if attempt:
            connection_ui._fail_attempt(
                attempt,
                stage="coexistence_phone_selection_server_error",
                message=message,
            )
        logger.exception(
            "Unexpected Coexistence phone-selection failure: org=%s attempt=%s",
            user.organization_id,
            getattr(attempt, "id", None),
        )
        messages.error(request, message)
        return redirect("whatsapp-connect-coexistence")

    _clear_pending_selection(request)
    logger.info(
        "Coexistence connected after explicit phone selection: org=%s attempt=%s account=%s phone=%s",
        user.organization_id,
        getattr(attempt, "id", None),
        account.id,
        selected_phone_id,
    )
    return _finish_success(
        request,
        account=account,
        warning=warning,
        sync_results=sync_results,
    )
