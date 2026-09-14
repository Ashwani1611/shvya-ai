"""Dedicated UI endpoints for WhatsApp Business App Coexistence."""

import logging

from django.conf import settings
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.crm.decorators import crm_login_required
from services.channels.embedded_signup_service import EmbeddedSignupError
from services.channels.whatsapp_coexistence_service import (
    COEXISTENCE_FINISH_EVENT,
    complete_coexistence_signup,
)

from . import connection_ui, views_flat
from .connection_attempts import WhatsAppConnectionAttempt

logger = logging.getLogger(__name__)


def whatsapp_connect_api_entry_view(request):
    """Keep the existing Connect API page but route its Coexistence CTA correctly."""
    response = connection_ui.whatsapp_connect_api_view(request)
    if (
        request.method == "GET"
        and getattr(response, "status_code", 500) == 200
        and "text/html" in response.get("Content-Type", "").lower()
        and not getattr(response, "streaming", False)
    ):
        try:
            html = response.content.decode(response.charset or "utf-8")
        except (AttributeError, UnicodeDecodeError):
            return response
        hosted_href = reverse("whatsapp-connect-hosted")
        coexistence_href = reverse("whatsapp-connect-coexistence")
        needle = f'href="{hosted_href}"'
        if needle in html:
            # whatsapp_connect_api.html has exactly one Hosted href: the card
            # currently labelled Coexistence. Rewriting that rendered link keeps
            # Hosted Account navigation elsewhere completely independent.
            html = html.replace(needle, f'href="{coexistence_href}"', 1)
            response.content = html.encode(response.charset or "utf-8")
            if response.has_header("Content-Length"):
                response["Content-Length"] = str(len(response.content))
    return response


@crm_login_required
def whatsapp_connect_coexistence_view(request):
    """Launch Meta's dedicated Business App Coexistence Embedded Signup flow."""
    user = request.crm_user
    if not views_flat._admin_required(user):
        return JsonResponse(
            {"error": "Only organization admins can connect WhatsApp."},
            status=403,
        )

    response = render(
        request,
        "channels/whatsapp_connect_coexistence.html",
        {
            "meta_app_id": settings.META_APP_ID,
            "meta_config_id": settings.META_WA_EMBEDDED_SIGNUP_CONFIG_ID,
            "embedded_signup_available": bool(
                settings.META_APP_ID and settings.META_WA_EMBEDDED_SIGNUP_CONFIG_ID
            ),
        },
    )
    return connection_ui._add_meta_resource_hints(response)


@crm_login_required
@require_POST
def whatsapp_coexistence_callback_view(request):
    """Complete Coexistence OAuth, subscribe webhooks, and request initial sync."""
    user = request.crm_user
    if not views_flat._admin_required(user):
        response = JsonResponse(
            {"error": "Only organization admins can connect WhatsApp."},
            status=403,
        )
        response["X-SHVYA-Toast"] = "off"
        return response

    attempt = connection_ui._get_or_create_attempt(
        user=user,
        method=WhatsAppConnectionAttempt.Method.EMBEDDED,
        attempt_id=request.POST.get("attempt_id"),
    )
    code = (request.POST.get("code") or "").strip()
    waba_id = (request.POST.get("waba_id") or "").strip()
    phone_number_id = (request.POST.get("phone_number_id") or "").strip()
    finish_event = (request.POST.get("finish_event") or "").strip()

    if finish_event != COEXISTENCE_FINISH_EVENT:
        message = (
            "Meta did not complete the WhatsApp Business App Coexistence flow. "
            "Please choose your existing WhatsApp Business App number and finish "
            "the QR/in-app confirmation before continuing."
        )
        connection_ui._fail_attempt(
            attempt,
            stage="coexistence_finish_event",
            message=message,
        )
        response = JsonResponse({"error": message}, status=400)
        response["X-SHVYA-Toast"] = "off"
        return response

    connection_ui._set_attempt(
        attempt,
        status=WhatsAppConnectionAttempt.Status.CALLBACK_RECEIVED,
        stage="coexistence_callback_received",
        code_received=bool(code),
        waba_id=waba_id or attempt.waba_id,
        phone_number_id=phone_number_id or attempt.phone_number_id,
        completed_at=None,
        error_message="",
    )

    try:
        account, warning, sync_results = complete_coexistence_signup(
            organization=user.organization,
            code=code,
            waba_id=waba_id,
            phone_number_id=phone_number_id,
            attempt=attempt,
        )
    except EmbeddedSignupError as exc:
        connection_ui._fail_attempt(
            attempt,
            stage=exc.stage or "coexistence_signup",
            message=str(exc),
            meta_error_code=exc.meta_error_code,
        )
        logger.warning(
            "WhatsApp Coexistence signup failed: org=%s attempt=%s stage=%s code=%s reason=%s",
            user.organization_id,
            attempt.id,
            exc.stage,
            exc.meta_error_code,
            exc,
        )
        response = JsonResponse(
            {
                "error": str(exc),
                "failure_stage": exc.stage,
                "attempt_id": str(attempt.id),
            },
            status=502,
        )
        response["X-SHVYA-Toast"] = "off"
        return response
    except Exception:
        connection_ui._fail_attempt(
            attempt,
            stage="coexistence_server_error",
            message="Unexpected server error while completing WhatsApp Coexistence setup.",
        )
        logger.exception(
            "Unexpected WhatsApp Coexistence signup failure: org=%s attempt=%s",
            user.organization_id,
            attempt.id,
        )
        response = JsonResponse(
            {
                "error": "Unexpected server error while completing WhatsApp Coexistence setup.",
                "failure_stage": "coexistence_server_error",
                "attempt_id": str(attempt.id),
            },
            status=502,
        )
        response["X-SHVYA-Toast"] = "off"
        return response

    if warning:
        messages.warning(request, warning)
    else:
        messages.success(
            request,
            "WhatsApp Business App Coexistence connected successfully.",
        )

    response = JsonResponse(
        {
            "ok": True,
            "redirect_url": f"{reverse('whatsapp-accounts')}?connected={account.id}",
            "attempt_id": str(attempt.id),
            "account_id": str(account.id),
            "waba_id": account.waba_id or "",
            "phone_number_id": account.phone_number_id or "",
            "phone_number": account.display_phone_number or "",
            "business_name": account.business_name or "",
            "warning": warning,
            "sync_requested": {
                "contacts": bool(sync_results.get("smb_app_state_sync")),
                "history": bool(sync_results.get("history")),
            },
        }
    )
    response["X-SHVYA-Toast"] = "off"
    response["Cache-Control"] = "no-store"
    return response
