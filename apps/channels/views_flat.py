import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.contrib import messages
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.accounts.models import User
from apps.crm.decorators import crm_login_required
from apps.crm.models import Lead

from .forms import (
    BulkCampaignForm,
    WhatsAppConnectAPIForm,
    WhatsAppHostedRequestForm,
    WhatsAppTemplateForm,
)
from .models import BulkMessageCampaign, WhatsAppAccount, WhatsAppTemplate

logger = logging.getLogger(__name__)


def _admin_required(user):
    """
    Connecting WhatsApp is an org-configuration action, not a
    day-to-day agent action -- restrict it to admins.
    """
    return user.role == User.Role.ADMIN


# ============================================================
# CHOICE SCREEN -- Connect API vs coexisted Account
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

    existing_accounts = WhatsAppAccount.objects.filter(
        organization=user.organization,
    )

    return render(
        request,
        "channels/whatsapp_connect_choice.html",
        {
            "existing_accounts": existing_accounts,
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

            account = form.save(organization=user.organization)

            # The manual credentials form only creates the
            # WhatsAppAccount row -- it doesn't tell Meta to actually
            # start sending webhook events for this WABA. Without
            # this call, the number connects fine and can SEND
            # messages, but inbound messages/statuses never arrive
            # and the Chats page stays empty forever, with nothing
            # in the UI to explain why.
            if account.waba_id:

                from apps.channels.providers.whatsapp import (
                    WhatsAppAPIError,
                    subscribe_app_to_waba,
                )

                try:
                    subscribe_app_to_waba(
                        waba_id=account.waba_id,
                        access_token=account.access_token,
                    )

                except WhatsAppAPIError as exc:
                    logger.warning(
                        "Failed to subscribe app to WABA %s for org %s: %s",
                        account.waba_id,
                        user.organization_id,
                        exc,
                    )

                    messages.warning(
                        request,
                        "WhatsApp connected, but SHVYA couldn't subscribe to "
                        "message notifications for it -- incoming chats won't "
                        "show up until this is fixed. Double-check the access "
                        "token has whatsapp_business_management permission, "
                        "then reconnect.",
                    )

                    return redirect("whatsapp-accounts")

            else:

                messages.warning(
                    request,
                    "WhatsApp connected, but no WhatsApp Business Account ID "
                    "was provided, so SHVYA couldn't subscribe to message "
                    "notifications -- incoming chats won't show up. Add the "
                    "WABA ID and reconnect to fix this.",
                )

                return redirect("whatsapp-accounts")

            messages.success(
                request,
                "WhatsApp connected successfully.",
            )

            return redirect("whatsapp-accounts")

    else:
        form = WhatsAppConnectAPIForm()

    return render(
        request,
        "channels/whatsapp_connect_api.html",
        {
            "form": form,
            "meta_app_id": settings.META_APP_ID,
            "meta_config_id": settings.META_WA_EMBEDDED_SIGNUP_CONFIG_ID,
            "embedded_signup_available": bool(
                settings.META_APP_ID and settings.META_WA_EMBEDDED_SIGNUP_CONFIG_ID
            ),
        },
    )


@crm_login_required
@require_POST
def whatsapp_embedded_signup_callback_view(request):
    """
    Called from the browser (fetch, not a form submit) once Meta's
    embedded signup popup finishes and hands back a `code`,
    `waba_id`, and `phone_number_id` -- see the JS in
    whatsapp_connect_api.html. Returns JSON; the page itself
    handles the redirect on success.
    """
    from services.channels.whatsapp_service import (
        WhatsAppEmbeddedSignupError,
        complete_embedded_signup,
    )

    user = request.crm_user

    if not _admin_required(user):
        return JsonResponse(
            {"error": "Only organization admins can connect WhatsApp."},
            status=403,
        )

    code = request.POST.get("code", "").strip()
    waba_id = request.POST.get("waba_id", "").strip()
    phone_number_id = request.POST.get("phone_number_id", "").strip()

    if not code or not waba_id or not phone_number_id:
        return JsonResponse(
            {"error": "Meta's popup didn't return all required details. Please try again."},
            status=400,
        )

    try:
        account = complete_embedded_signup(
            organization=user.organization,
            code=code,
            waba_id=waba_id,
            phone_number_id=phone_number_id,
        )

    except WhatsAppEmbeddedSignupError as exc:
        logger.warning("Embedded signup failed for org %s: %s", user.organization_id, exc)

        return JsonResponse(
            {"error": str(exc)},
            status=502,
        )

    return JsonResponse(
        {
            "redirect_url": reverse("whatsapp-accounts"),
            "account_id": str(account.id),
        }
    )


# ============================================================
# coexisted ACCOUNT -- SHVYA provisions the number
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
                "coexisted WhatsApp number requested. "
                "SHVYA will provision it and notify you when it's ready.",
            )

            return redirect("whatsapp-accounts")

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
def whatsapp_account_list_view(request):
    """
    Lists every WhatsApp number connected to this organization
    (Kraya-style "Connect API" table: Mobile / Business Name /
    Status / Welcome Message / Request Contact Info / Action).
    """
    user = request.crm_user

    accounts = WhatsAppAccount.objects.filter(
        organization=user.organization,
    )

    return render(
        request,
        "channels/whatsapp_account_list.html",
        {
            "accounts": accounts,
            "can_manage": _admin_required(user),
        },
    )


# ============================================================
# DISCONNECT
# ============================================================


@crm_login_required
@require_POST
def whatsapp_disconnect_view(request, account_id):

    user = request.crm_user

    if not _admin_required(user):
        messages.error(
            request,
            "Only organization admins can manage WhatsApp connections.",
        )
        return redirect("whatsapp-accounts")

    updated = WhatsAppAccount.objects.filter(
        id=account_id,
        organization=user.organization,
    ).update(
        status=WhatsAppAccount.Status.DISCONNECTED,
        is_active=False,
    )

    if updated:
        messages.success(request, "WhatsApp account disconnected.")
    else:
        messages.error(request, "Account not found.")

    return redirect("whatsapp-accounts")


@crm_login_required
@require_POST
def whatsapp_resubscribe_view(request, account_id):
    """
    Re-sends the "subscribe this app to that WABA's webhooks" call
    for an ALREADY-connected account. Exists because accounts
    connected via the manual credentials form before this endpoint
    was added (or any account where that call failed the first
    time) can otherwise never receive inbound messages/statuses --
    there was no way to fix that without disconnecting and
    reconnecting, which would have created a duplicate account row
    since phone_number_id isn't unique on this model.
    """
    from apps.channels.providers.whatsapp import (
        WhatsAppAPIError,
        subscribe_app_to_waba,
    )

    user = request.crm_user

    if not _admin_required(user):
        messages.error(
            request,
            "Only organization admins can manage WhatsApp connections.",
        )
        return redirect("whatsapp-accounts")

    account = WhatsAppAccount.objects.filter(
        id=account_id,
        organization=user.organization,
    ).first()

    if not account:
        messages.error(request, "Account not found.")
        return redirect("whatsapp-accounts")

    if not account.waba_id:
        messages.error(
            request,
            "This account has no WhatsApp Business Account ID on file, "
            "so SHVYA can't subscribe to its message notifications. "
            "Disconnect it and reconnect with the WABA ID filled in.",
        )
        return redirect("whatsapp-accounts")

    try:
        subscribe_app_to_waba(
            waba_id=account.waba_id,
            access_token=account.access_token,
        )

    except WhatsAppAPIError as exc:
        logger.warning(
            "Resubscribe failed for account %s (org %s): %s",
            account.id,
            user.organization_id,
            exc,
        )

        messages.error(
            request,
            "Couldn't subscribe to message notifications: "
            f"{exc}. Check that the access token still has "
            "whatsapp_business_management permission.",
        )
        return redirect("whatsapp-accounts")

    messages.success(
        request,
        "Subscribed to message notifications. New incoming messages "
        "should now show up in Chats -- messages sent to this number "
        "before now won't retroactively appear.",
    )

    return redirect("whatsapp-accounts")


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

    from apps.channels.tasks import send_whatsapp_message_task
    from services.channels.whatsapp_service import (
        queue_outbound_message,
        resolve_account_for_lead,
    )

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

    # Picks the number this lead already has a conversation on, or
    # the pipeline's configured number, or the org's first connected
    # account -- an org can have several connected numbers now, so
    # this is no longer a bare .first().
    account = resolve_account_for_lead(
        organization=user.organization,
        lead=lead,
    )

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


# ============================================================
# BULK CAMPAIGNS
# ============================================================


@crm_login_required
@require_GET
def whatsapp_campaign_list_view(request):

    user = request.crm_user

    campaigns = BulkMessageCampaign.objects.filter(
        organization=user.organization,
    ).select_related("pipeline", "stage", "created_by")

    return render(
        request,
        "channels/whatsapp_campaign_list.html",
        {
            "campaigns": campaigns,
            "can_manage": _admin_required(user),
        },
    )


@crm_login_required
def whatsapp_campaign_create_view(request):

    from services.channels.bulk_service import BulkCampaignError, create_campaign

    user = request.crm_user

    if not _admin_required(user):
        messages.error(
            request,
            "Only organization admins can create bulk campaigns.",
        )
        return redirect("whatsapp-campaign-list")

    if request.method == "POST":

        form = BulkCampaignForm(request.POST, organization=user.organization)

        if form.is_valid():

            try:
                campaign = create_campaign(
                    organization=user.organization,
                    created_by=user,
                    name=form.cleaned_data["name"],
                    account=form.cleaned_data["account"],
                    pipeline=form.cleaned_data["pipeline"],
                    stage=form.cleaned_data.get("stage"),
                    tag=form.cleaned_data.get("tag"),
                    body=form.cleaned_data["body"],
                    template_name=form.cleaned_data.get("template_name", ""),
                )

            except BulkCampaignError as exc:
                form.add_error(None, str(exc))

            else:
                messages.success(
                    request,
                    f"Campaign \"{campaign.name}\" created with "
                    f"{campaign.recipients.count()} recipients. Review and launch it below.",
                )
                return redirect("whatsapp-campaign-detail", campaign_id=campaign.id)

    else:
        form = BulkCampaignForm(organization=user.organization)

    return render(
        request,
        "channels/whatsapp_campaign_create.html",
        {
            "form": form,
        },
    )


@crm_login_required
@require_GET
def whatsapp_campaign_detail_view(request, campaign_id):

    user = request.crm_user

    campaign = (
        BulkMessageCampaign.objects.filter(
            id=campaign_id,
            organization=user.organization,
        )
        .select_related("pipeline", "stage", "created_by")
        .first()
    )

    if not campaign:
        messages.error(request, "Campaign not found.")
        return redirect("whatsapp-campaign-list")

    recipients = campaign.recipients.select_related("lead", "message")

    return render(
        request,
        "channels/whatsapp_campaign_detail.html",
        {
            "campaign": campaign,
            "recipients": recipients,
            "can_manage": _admin_required(user),
        },
    )


@crm_login_required
@require_POST
def whatsapp_campaign_launch_view(request, campaign_id):

    from apps.channels.tasks import send_bulk_campaign_task
    from services.channels.bulk_service import BulkCampaignError, launch_campaign

    user = request.crm_user

    if not _admin_required(user):
        messages.error(
            request,
            "Only organization admins can launch bulk campaigns.",
        )
        return redirect("whatsapp-campaign-detail", campaign_id=campaign_id)

    campaign = BulkMessageCampaign.objects.filter(
        id=campaign_id,
        organization=user.organization,
    ).first()

    if not campaign:
        messages.error(request, "Campaign not found.")
        return redirect("whatsapp-campaign-list")

    try:
        launch_campaign(campaign=campaign)

    except BulkCampaignError as exc:
        messages.error(request, str(exc))
        return redirect("whatsapp-campaign-detail", campaign_id=campaign_id)

    # NOTE: same Celery wiring caveat as whatsapp_send_message_view --
    # this will raise until config/celery.py is actually activated.
    send_bulk_campaign_task.delay(str(campaign.id))

    messages.success(request, f"Campaign \"{campaign.name}\" launched.")
    return redirect("whatsapp-campaign-detail", campaign_id=campaign_id)


# ============================================================
# MESSAGE TEMPLATES
# ============================================================


@crm_login_required
@require_GET
def whatsapp_template_list_view(request):
    """
    Filterable template list matching Kraya's screen: filter by
    Category, Status, and Business (which connected account).
    """
    user = request.crm_user

    templates = WhatsAppTemplate.objects.filter(
        organization=user.organization,
    ).select_related("account")

    category = request.GET.get("category")
    status = request.GET.get("status")
    account_id = request.GET.get("account")

    if category:
        templates = templates.filter(category=category)

    if status:
        templates = templates.filter(status=status)

    if account_id:
        templates = templates.filter(account_id=account_id)

    accounts = WhatsAppAccount.objects.filter(
        organization=user.organization,
    )

    return render(
        request,
        "channels/whatsapp_template_list.html",
        {
            "templates": templates,
            "accounts": accounts,
            "categories": WhatsAppTemplate.Category.choices,
            "statuses": WhatsAppTemplate.Status.choices,
            "selected_category": category or "",
            "selected_status": status or "",
            "selected_account": account_id or "",
            "can_manage": _admin_required(user),
        },
    )


@crm_login_required
def whatsapp_template_create_view(request):

    from services.channels.template_service import (
        AVAILABLE_VARIABLES,
        TemplateError,
        create_template,
    )

    user = request.crm_user

    if not _admin_required(user):
        messages.error(
            request,
            "Only organization admins can create message templates.",
        )
        return redirect("whatsapp-template-list")

    if request.method == "POST":

        form = WhatsAppTemplateForm(request.POST, organization=user.organization)

        if form.is_valid():

            try:
                template = create_template(
                    organization=user.organization,
                    account=form.cleaned_data["account"],
                    created_by=user,
                    name=form.cleaned_data["name"],
                    body=form.cleaned_data["body"],
                    category=form.cleaned_data["category"],
                    template_format=form.cleaned_data["template_format"],
                    footer=form.cleaned_data.get("footer", ""),
                    attachment_type=form.cleaned_data["attachment_type"],
                    buttons=form.cleaned_data.get("buttons", []),
                )

            except TemplateError as exc:
                form.add_error(None, str(exc))

            else:
                messages.success(
                    request,
                    f"Template \"{template.name}\" saved as draft.",
                )
                return redirect("whatsapp-template-list")

    else:
        form = WhatsAppTemplateForm(organization=user.organization)

    return render(
        request,
        "channels/whatsapp_template_create.html",
        {
            "form": form,
            "available_variables": AVAILABLE_VARIABLES,
        },
    )


# ============================================================
# CHATS INBOX
# ============================================================


@crm_login_required
@require_GET
def whatsapp_chat_list_view(request):

    from services.channels.whatsapp_service import list_conversations

    user = request.crm_user

    account_id = request.GET.get("account")
    account = None

    if account_id:
        account = WhatsAppAccount.objects.filter(
            id=account_id,
            organization=user.organization,
        ).first()

    conversations = list_conversations(
        organization=user.organization,
        account=account,
    )

    accounts = WhatsAppAccount.objects.filter(
        organization=user.organization,
        status=WhatsAppAccount.Status.CONNECTED,
    )

    return render(
        request,
        "channels/whatsapp_chat_list.html",
        {
            "conversations": conversations,
            "accounts": accounts,
            "selected_account": account,
        },
    )


@crm_login_required
@require_GET
def whatsapp_chat_detail_view(request, lead_id):

    from services.channels.whatsapp_service import (
        get_conversation_messages,
        mark_conversation_read,
    )

    user = request.crm_user

    lead = Lead.objects.filter(
        id=lead_id,
        organization=user.organization,
    ).first()

    if not lead:
        messages.error(request, "Lead not found.")
        return redirect("whatsapp-chats")

    chat_messages = get_conversation_messages(
        organization=user.organization,
        lead=lead,
    )

    mark_conversation_read(organization=user.organization, lead=lead)

    return render(
        request,
        "channels/whatsapp_chat_detail.html",
        {
            "lead": lead,
            "chat_messages": chat_messages,
        },
    )