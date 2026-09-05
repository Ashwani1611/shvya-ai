"""Connection-aware UI wrappers for WhatsApp API onboarding.

There are two independent ways to populate the same final WhatsAppAccount:

* Meta Embedded Signup -- browser receives a code/WABA/phone id and SHVYA
  exchanges the code server-side.
* Manual Access Token -- an admin supplies phone id/WABA/token directly.

The paths do not call each other. They intentionally converge only at the final
WhatsAppAccount record. WhatsAppConnectionAttempt keeps a separate audit trail
for both methods without ever storing raw OAuth codes or access-token values.
"""

import logging
import uuid

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.crm.decorators import crm_login_required
from services.channels.embedded_signup_service import (
    EmbeddedSignupError,
    complete_embedded_signup,
)

from . import views_flat
from .connection_attempts import WhatsAppConnectionAttempt
from .models import WhatsAppAccount

logger = logging.getLogger(__name__)


_STATUS_RANK = {
    WhatsAppConnectionAttempt.Status.STARTED: 0,
    WhatsAppConnectionAttempt.Status.META_FINISHED: 1,
    WhatsAppConnectionAttempt.Status.CODE_RECEIVED: 2,
    WhatsAppConnectionAttempt.Status.CALLBACK_RECEIVED: 3,
    WhatsAppConnectionAttempt.Status.TOKEN_EXCHANGED: 4,
    WhatsAppConnectionAttempt.Status.PHONE_VERIFIED: 5,
    WhatsAppConnectionAttempt.Status.CONNECTED: 6,
}


def _has_connected_api_account(user):
    return WhatsAppAccount.objects.filter(
        organization=user.organization,
        connection_type=WhatsAppAccount.ConnectionType.API,
        status=WhatsAppAccount.Status.CONNECTED,
        is_active=True,
    ).exists()


def _attempt_id(value):
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _create_attempt_with_id(*, parsed_id, user, method):
    """Create a caller-supplied attempt id safely under concurrent requests."""
    try:
        with transaction.atomic():
            return WhatsAppConnectionAttempt.objects.create(
                id=parsed_id,
                organization=user.organization,
                created_by=user,
                method=method,
                status=WhatsAppConnectionAttempt.Status.STARTED,
                stage="started",
            )
    except IntegrityError:
        # The browser telemetry request and final callback can race. If both
        # try to create the same UUID, the loser simply re-reads the winner.
        return WhatsAppConnectionAttempt.objects.filter(
            id=parsed_id,
            organization=user.organization,
        ).first()


def _get_or_create_attempt(*, user, method, attempt_id=None):
    """Return an org-scoped attempt; never attach another tenant's UUID."""
    parsed_id = _attempt_id(attempt_id)

    if parsed_id:
        attempt = WhatsAppConnectionAttempt.objects.filter(
            id=parsed_id,
            organization=user.organization,
        ).first()
        if attempt:
            if attempt.created_by_id is None:
                attempt.created_by = user
                attempt.save(update_fields=["created_by", "updated_at"])
            return attempt

        # A UUID from another organization must never be reused. UUID collision
        # is practically impossible, but this also protects against tampering.
        if not WhatsAppConnectionAttempt.objects.filter(id=parsed_id).exists():
            attempt = _create_attempt_with_id(
                parsed_id=parsed_id,
                user=user,
                method=method,
            )
            if attempt:
                return attempt

    return WhatsAppConnectionAttempt.objects.create(
        organization=user.organization,
        created_by=user,
        method=method,
        status=WhatsAppConnectionAttempt.Status.STARTED,
        stage="started",
    )


def _set_attempt(attempt, **changes):
    for field, value in changes.items():
        setattr(attempt, field, value)
    attempt.save()


def _fail_attempt(attempt, *, stage, message, meta_error_code=""):
    _set_attempt(
        attempt,
        status=WhatsAppConnectionAttempt.Status.FAILED,
        stage=stage,
        meta_error_code=str(meta_error_code or ""),
        error_message=(message or "")[:4000],
        completed_at=timezone.now(),
    )


def _add_meta_resource_hints(response):
    """Warm Meta origins before the user clicks Connect API.

    The SDK itself still loads through the page's existing async loader so we
    do not introduce an fbAsyncInit race. Preconnect removes much of the DNS/TLS
    setup that otherwise makes the first popup noticeably slow.
    """
    content_type = response.get("Content-Type", "")
    if "text/html" not in content_type.lower() or getattr(response, "streaming", False):
        return response

    try:
        html = response.content.decode(response.charset or "utf-8")
    except (AttributeError, UnicodeDecodeError):
        return response

    if "meta-whatsapp-resource-hints" in html or "</head>" not in html.lower():
        return response

    hints = """
<!-- meta-whatsapp-resource-hints -->
<link rel="dns-prefetch" href="//connect.facebook.net">
<link rel="dns-prefetch" href="//www.facebook.com">
<link rel="preconnect" href="https://connect.facebook.net" crossorigin>
<link rel="preconnect" href="https://www.facebook.com" crossorigin>
"""
    lower = html.lower()
    index = lower.rfind("</head>")
    html = html[:index] + hints + html[index:]
    response.content = html.encode(response.charset or "utf-8")
    if response.has_header("Content-Length"):
        response["Content-Length"] = str(len(response.content))
    return response


@crm_login_required
def whatsapp_connect_api_view(request):
    """Show onboarding or process the independent manual-token path."""
    user = request.crm_user

    if (
        request.method == "GET"
        and _has_connected_api_account(user)
        and request.GET.get("add") != "1"
    ):
        return redirect("whatsapp-accounts")

    manual_attempt = None
    if request.method == "POST" and views_flat._admin_required(user):
        manual_attempt = WhatsAppConnectionAttempt.objects.create(
            organization=user.organization,
            created_by=user,
            method=WhatsAppConnectionAttempt.Method.MANUAL,
            status=WhatsAppConnectionAttempt.Status.STARTED,
            stage="manual_submit",
            waba_id=(request.POST.get("waba_id") or "").strip()[:64],
            phone_number_id=(request.POST.get("phone_number_id") or "").strip()[:64],
            display_phone_number=(request.POST.get("display_phone_number") or "").strip()[:32],
            token_received=bool((request.POST.get("access_token") or "").strip()),
        )
        logger.info(
            "Manual WhatsApp connection submitted: org=%s attempt=%s waba_id=%s phone_number_id=%s token_received=%s",
            user.organization_id,
            manual_attempt.id,
            manual_attempt.waba_id,
            manual_attempt.phone_number_id,
            manual_attempt.token_received,
        )

    try:
        response = views_flat.whatsapp_connect_api_view(request)
    except Exception:
        if manual_attempt:
            _fail_attempt(
                manual_attempt,
                stage="manual_server_error",
                message="Server error while processing the manual WhatsApp connection.",
            )
        logger.exception(
            "Manual WhatsApp connection crashed: org=%s attempt=%s",
            user.organization_id,
            getattr(manual_attempt, "id", None),
        )
        raise

    if manual_attempt:
        account = (
            WhatsAppAccount.objects.filter(
                organization=user.organization,
                phone_number_id=manual_attempt.phone_number_id,
                status=WhatsAppAccount.Status.CONNECTED,
                is_active=True,
            )
            .order_by("-updated_at")
            .first()
        )

        if 300 <= response.status_code < 400 and account:
            _set_attempt(
                manual_attempt,
                account=account,
                status=WhatsAppConnectionAttempt.Status.CONNECTED,
                stage="account_saved",
                waba_id=account.waba_id,
                phone_number_id=account.phone_number_id,
                display_phone_number=account.display_phone_number,
                business_name=account.business_name,
                completed_at=timezone.now(),
            )
            logger.info(
                "Manual WhatsApp connection saved: org=%s attempt=%s account_id=%s phone_number_id=%s",
                user.organization_id,
                manual_attempt.id,
                account.id,
                account.phone_number_id,
            )
        elif response.status_code < 500:
            _fail_attempt(
                manual_attempt,
                stage="manual_validation",
                message="Manual WhatsApp credentials were not accepted by the connection form.",
            )

    if request.method == "GET" and response.status_code == 200:
        response = _add_meta_resource_hints(response)

    return response


@crm_login_required
@require_POST
def whatsapp_connection_attempt_event_view(request):
    """Persist safe browser-side Embedded Signup lifecycle events."""
    user = request.crm_user
    if not views_flat._admin_required(user):
        return JsonResponse({"error": "Only organization admins can connect WhatsApp."}, status=403)

    attempt = _get_or_create_attempt(
        user=user,
        method=WhatsAppConnectionAttempt.Method.EMBEDDED,
        attempt_id=request.POST.get("attempt_id"),
    )

    stage = (request.POST.get("stage") or "started").strip()[:64]
    allowed_stages = {
        "started",
        "meta_finish",
        "oauth_code_received",
        "oauth_code_missing",
        "incomplete",
        "cancelled",
        "meta_error",
    }
    if stage not in allowed_stages:
        stage = "started"

    requested_status = {
        "started": WhatsAppConnectionAttempt.Status.STARTED,
        "meta_finish": WhatsAppConnectionAttempt.Status.META_FINISHED,
        "oauth_code_received": WhatsAppConnectionAttempt.Status.CODE_RECEIVED,
        "oauth_code_missing": WhatsAppConnectionAttempt.Status.STARTED,
        "incomplete": WhatsAppConnectionAttempt.Status.FAILED,
        "cancelled": WhatsAppConnectionAttempt.Status.CANCELLED,
        "meta_error": WhatsAppConnectionAttempt.Status.FAILED,
    }[stage]

    current_terminal = attempt.status in {
        WhatsAppConnectionAttempt.Status.CONNECTED,
        WhatsAppConnectionAttempt.Status.FAILED,
        WhatsAppConnectionAttempt.Status.CANCELLED,
    }

    changes = {}
    if not current_terminal:
        current_rank = _STATUS_RANK.get(attempt.status, 0)
        requested_rank = _STATUS_RANK.get(requested_status, current_rank)
        if requested_status in {
            WhatsAppConnectionAttempt.Status.FAILED,
            WhatsAppConnectionAttempt.Status.CANCELLED,
        } or requested_rank >= current_rank:
            changes["status"] = requested_status
            changes["stage"] = stage

    waba_id = (request.POST.get("waba_id") or "").strip()[:64]
    phone_number_id = (request.POST.get("phone_number_id") or "").strip()[:64]
    if waba_id:
        changes["waba_id"] = waba_id
    if phone_number_id:
        changes["phone_number_id"] = phone_number_id
    if request.POST.get("code_received") in {"1", "true", "True"}:
        changes["code_received"] = True

    if stage in {"incomplete", "cancelled", "meta_error"} and not current_terminal:
        changes["error_message"] = (request.POST.get("error_message") or "")[:4000]
        changes["meta_error_code"] = (request.POST.get("meta_error_code") or "")[:64]
        changes["completed_at"] = timezone.now()

    if changes:
        _set_attempt(attempt, **changes)

    logger.info(
        "Embedded signup browser stage: org=%s attempt=%s stage=%s status=%s code_received=%s waba_id=%s phone_number_id=%s",
        user.organization_id,
        attempt.id,
        stage,
        attempt.status,
        attempt.code_received,
        attempt.waba_id,
        attempt.phone_number_id,
    )

    response = JsonResponse(
        {
            "ok": True,
            "attempt_id": str(attempt.id),
            "status": attempt.status,
            "stage": attempt.stage,
        }
    )
    response["X-SHVYA-Toast"] = "off"
    response["Cache-Control"] = "no-store"
    return response


@crm_login_required
@require_POST
def whatsapp_embedded_signup_callback_view(request):
    """Finish Meta Embedded Signup and persist the connected account."""
    user = request.crm_user

    if not views_flat._admin_required(user):
        response = JsonResponse(
            {"error": "Only organization admins can connect WhatsApp."},
            status=403,
        )
        response["X-SHVYA-Toast"] = "off"
        return response

    attempt = _get_or_create_attempt(
        user=user,
        method=WhatsAppConnectionAttempt.Method.EMBEDDED,
        attempt_id=request.POST.get("attempt_id"),
    )

    code = (request.POST.get("code") or "").strip()
    waba_id = (request.POST.get("waba_id") or "").strip()
    phone_number_id = (request.POST.get("phone_number_id") or "").strip()

    logger.info(
        "Embedded signup callback received: org=%s attempt=%s code_received=%s waba_id=%s phone_number_id=%s",
        user.organization_id,
        attempt.id,
        bool(code),
        waba_id,
        phone_number_id,
    )

    _set_attempt(
        attempt,
        status=WhatsAppConnectionAttempt.Status.CALLBACK_RECEIVED,
        stage="callback_received",
        code_received=bool(code) or attempt.code_received,
        waba_id=waba_id or attempt.waba_id,
        phone_number_id=phone_number_id or attempt.phone_number_id,
        completed_at=None,
        error_message="",
    )

    missing = []
    if not code:
        missing.append("authorization code")
    if not waba_id:
        missing.append("WABA ID")
    if not phone_number_id:
        missing.append("Phone Number ID")

    if missing:
        message = "Meta's signup did not return: " + ", ".join(missing) + ". Please reconnect and try again."
        _fail_attempt(attempt, stage="callback_validation", message=message)
        response = JsonResponse({"error": message, "attempt_id": str(attempt.id)}, status=400)
        response["X-SHVYA-Toast"] = "off"
        return response

    try:
        account, warning = complete_embedded_signup(
            organization=user.organization,
            code=code,
            waba_id=waba_id,
            phone_number_id=phone_number_id,
            attempt=attempt,
        )
    except EmbeddedSignupError as exc:
        _fail_attempt(
            attempt,
            stage=exc.stage or "embedded_signup",
            message=str(exc),
            meta_error_code=exc.meta_error_code,
        )
        logger.warning(
            "Embedded signup failed: org=%s attempt=%s stage=%s meta_code=%s reason=%s",
            user.organization_id,
            attempt.id,
            exc.stage,
            exc.meta_error_code,
            exc,
        )
        response = JsonResponse(
            {
                "error": str(exc),
                "attempt_id": str(attempt.id),
                "failure_stage": exc.stage,
            },
            status=502,
        )
        response["X-SHVYA-Toast"] = "off"
        return response
    except Exception:
        _fail_attempt(
            attempt,
            stage="server_error",
            message="Unexpected server error while completing WhatsApp connection.",
        )
        logger.exception(
            "Unexpected embedded signup failure: org=%s attempt=%s",
            user.organization_id,
            attempt.id,
        )
        response = JsonResponse(
            {
                "error": "Unexpected server error while completing WhatsApp connection.",
                "attempt_id": str(attempt.id),
                "failure_stage": "server_error",
            },
            status=502,
        )
        response["X-SHVYA-Toast"] = "off"
        return response

    if warning:
        messages.warning(request, warning)
    else:
        messages.success(request, "WhatsApp Business API connected successfully.")

    redirect_url = f"{reverse('whatsapp-accounts')}?connected={account.id}"
    response = JsonResponse(
        {
            "ok": True,
            "redirect_url": redirect_url,
            "attempt_id": str(attempt.id),
            "account_id": str(account.id),
            "waba_id": account.waba_id or "",
            "phone_number_id": account.phone_number_id or "",
            "phone_number": account.display_phone_number or "",
            "business_name": account.business_name or "",
            "subscription_warning": warning,
        }
    )
    response["X-SHVYA-Toast"] = "off"
    response["Cache-Control"] = "no-store"
    return response
