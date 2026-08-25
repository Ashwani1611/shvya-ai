import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.contrib import messages
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.accounts.models import User
from apps.crm.decorators import crm_login_required
from apps.crm.models import Lead

from .forms import WhatsAppConnectAPIForm, WhatsAppHostedRequestForm
from .models import WhatsAppAccount

logger = logging.getLogger(__name__)


def _admin_required(user):
    """
    Connecting WhatsApp is an org-configuration action, not a
    day-to-day agent action -- restrict it to admins.
    """
    return user.role == User.Role.ADMIN


# ============================================================
# CHOICE SCREEN -- Connect API vs Hosted Account
# ============================================================


@crm_login_required
@require_GET
def whatsapp_connect_choice_view(request):

    user = request.crm_user

    if not _admin_required(user):
        messages.error(
            request,
            "Only organization admins can manage WhatsApp connections.",
        )
        return redirect("crm-dashboard")

    existing_account = WhatsAppAccount.objects.filter(
        organization=user.organization,
    ).first()

    return render(
        request,
        "channels/whatsapp_connect_choice.html",
        {
            "existing_account": existing_account,
        },
    )


# ============================================================
# CONNECT API -- organization brings their own Meta credentials
# ============================================================


@crm_login_required
def whatsapp_connect_api_view(request):

    user = request.crm_user

    if not _admin_required(user):
        messages.error(
            request,
            "Only organization admins can manage WhatsApp connections.",
        )
        return redirect("crm-dashboard")

    if request.method == "POST":

        form = WhatsAppConnectAPIForm(request.POST)

        if form.is_valid():

            form.save(organization=user.organization)

            messages.success(
                request,
                "WhatsApp connected successfully.",
            )

            return redirect("whatsapp-settings")

    else:
        form = WhatsAppConnectAPIForm()

    return render(
        request,
        "channels/whatsapp_connect_api.html",
        {
            "form": form,
        },
    )


# ============================================================
# HOSTED ACCOUNT -- SHVYA provisions the number
# ============================================================


@crm_login_required
def whatsapp_connect_hosted_view(request):

    user = request.crm_user

    if not _admin_required(user):
        messages.error(
            request,
            "Only organization admins can manage WhatsApp connections.",
        )
        return redirect("crm-dashboard")

    if request.method == "POST":

        form = WhatsAppHostedRequestForm(request.POST)

        if form.is_valid():

            form.save(organization=user.organization)

            messages.success(
                request,
                "Hosted WhatsApp number requested. "
                "SHVYA will provision it and notify you when it's ready.",
            )

            return redirect("whatsapp-settings")

    else:
        form = WhatsAppHostedRequestForm()

    return render(
        request,
        "channels/whatsapp_connect_hosted.html",
        {
            "form": form,
        },
    )


# ============================================================
# SETTINGS -- current connection status for this org
# ============================================================


@crm_login_required
@require_GET
def whatsapp_settings_view(request):

    user = request.crm_user

    account = WhatsAppAccount.objects.filter(
        organization=user.organization,
    ).first()

    return render(
        request,
        "channels/whatsapp_settings.html",
        {
            "account": account,
            "can_manage": _admin_required(user),
        },
    )


# ============================================================
# DISCONNECT
# ============================================================


@crm_login_required
@require_POST
def whatsapp_disconnect_view(request):

    user = request.crm_user

    if not _admin_required(user):
        messages.error(
            request,
            "Only organization admins can manage WhatsApp connections.",
        )
        return redirect("whatsapp-settings")

    WhatsAppAccount.objects.filter(
        organization=user.organization,
    ).update(
        status=WhatsAppAccount.Status.DISCONNECTED,
        is_active=False,
    )

    messages.success(
        request,
        "WhatsApp account disconnected.",
    )

    return redirect("whatsapp-settings")


# ============================================================
# WEBHOOK -- Meta calls this, not a logged-in user
#
# GET  -> verification handshake (hub.challenge echo)
# POST -> actual message/status delivery
#
# csrf_exempt is required: Meta does not send a Django CSRF
# token. Authenticity is instead verified via the
# X-Hub-Signature-256 header (HMAC-SHA256 of the raw body,
# keyed with META_APP_SECRET).
# ============================================================


def _verify_signature(request):
    """
    Returns True if the request body is signed by Meta using our
    configured app secret. If META_APP_SECRET isn't configured
    yet, verification is skipped (logged loudly) rather than
    rejecting every webhook call outright during initial setup.
    """
    if not settings.META_APP_SECRET:
        logger.warning(
            "WhatsApp webhook: META_APP_SECRET not configured, "
            "skipping signature verification. Set this before going live."
        )
        return True

    signature_header = request.headers.get("X-Hub-Signature-256", "")

    if not signature_header.startswith("sha256="):
        return False

    expected = hmac.new(
        settings.META_APP_SECRET.encode("utf-8"),
        request.body,
        hashlib.sha256,
    ).hexdigest()

    provided = signature_header.removeprefix("sha256=")

    return hmac.compare_digest(expected, provided)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def whatsapp_webhook_view(request):

    if request.method == "GET":
        return _handle_webhook_verification(request)

    return _handle_webhook_delivery(request)


def _handle_webhook_verification(request):

    mode = request.GET.get("hub.mode")
    token = request.GET.get("hub.verify_token")
    challenge = request.GET.get("hub.challenge", "")

    if (
        mode == "subscribe"
        and settings.META_VERIFY_TOKEN
        and token == settings.META_VERIFY_TOKEN
    ):
        return HttpResponse(challenge)

    logger.warning("WhatsApp webhook verification failed.")
    return HttpResponseForbidden()


def _handle_webhook_delivery(request):

    from services.channels.whatsapp_service import (
        handle_inbound_message,
        handle_status_update,
    )

    if not _verify_signature(request):
        logger.warning("WhatsApp webhook: signature verification failed.")
        return HttpResponseForbidden()

    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return HttpResponse(status=400)

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):

            value = change.get("value", {})
            metadata = value.get("metadata", {})
            phone_number_id = metadata.get("phone_number_id")

            account = WhatsAppAccount.objects.filter(
                phone_number_id=phone_number_id,
            ).select_related("organization").first()

            if not account:
                logger.warning(
                    "WhatsApp webhook: no account for phone_number_id=%s",
                    phone_number_id,
                )
                continue

            for message in value.get("messages", []):

                body = ""
                if message.get("type") == "text":
                    body = message.get("text", {}).get("body", "")

                handle_inbound_message(
                    organization=account.organization,
                    account=account,
                    external_id=message.get("id"),
                    from_number=message.get("from", ""),
                    to_number=phone_number_id,
                    body=body,
                    raw_payload=message,
                )

            for status_event in value.get("statuses", []):

                handle_status_update(
                    external_id=status_event.get("id"),
                    status=status_event.get("status"),
                    raw_payload=status_event,
                )

    # Meta requires a 200 response regardless of content, or it
    # will keep retrying delivery of this same payload.
    return HttpResponse(status=200)


# ============================================================
# SEND MESSAGE -- triggered from the CRM (e.g. lead detail panel)
# ============================================================


@crm_login_required
@require_POST
def whatsapp_send_message_view(request, lead_id):

    from services.channels.whatsapp_service import queue_outbound_message
    from apps.channels.tasks import send_whatsapp_message_task

    user = request.crm_user

    lead = (
        Lead.objects.filter(
            id=lead_id,
            organization=user.organization,
        )
        .first()
    )

    if not lead:
        return JsonResponse({"error": "Lead not found."}, status=404)

    account = WhatsAppAccount.objects.filter(
        organization=user.organization,
        is_active=True,
        status=WhatsAppAccount.Status.CONNECTED,
    ).first()

    if not account:
        return JsonResponse(
            {"error": "No connected WhatsApp account for this organization."},
            status=400,
        )

    body = (request.POST.get("body") or "").strip()

    if not body:
        return JsonResponse({"error": "Message body is required."}, status=400)

    message = queue_outbound_message(
        organization=user.organization,
        account=account,
        to_number=lead.phone,
        body=body,
        lead=lead,
    )

    # NOTE: Celery is not wired into INSTALLED_APPS yet (see
    # apps/channels/tasks.py). Until that's done, this .delay()
    # call will raise, not silently no-op -- don't rely on this
    # endpoint working end-to-end before that wiring lands.
    send_whatsapp_message_task.delay(str(message.id))

    return JsonResponse(
        {
            "id": str(message.id),
            "status": message.status,
        },
        status=202,
    )