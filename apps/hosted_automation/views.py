import mimetypes

from django.contrib import messages
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.channels.models import WhatsAppAccount
from apps.crm.decorators import crm_login_required
from apps.followups.models import FollowupSequence, FollowupStep
from apps.hosted_automation.models import HostedFollowupStepConfig
from apps.organizations.features import is_hosted_account_enabled
from services.channels.hosted_automation_service import (
    MEDIA_TOKEN_MAX_AGE_SECONDS,
    HostedAutomationError,
    add_hosted_whatsapp_step,
    duplicate_hosted_configs,
    health_snapshot,
    set_health_enabled,
    update_hosted_whatsapp_step,
)
from services.followup_service import FollowupError, create_sequence, duplicate_sequence


def _admin_required(request):
    from apps.followups.views.web import _admin_required as original

    return original(request)


def _organization_sequence(request, sequence_id):
    return get_object_or_404(
        FollowupSequence.objects.select_related("whatsapp_account", "created_by"),
        id=sequence_id,
        organization=request.crm_user.organization,
    )


def _connected_accounts(user, connection_type):
    if connection_type == "hosted" and not is_hosted_account_enabled(user.organization):
        return WhatsAppAccount.objects.none()
    return WhatsAppAccount.objects.filter(
        organization=user.organization,
        connection_type=connection_type,
        status=WhatsAppAccount.Status.CONNECTED,
        is_active=True,
    ).order_by("business_name", "display_phone_number")


@crm_login_required
@require_GET
def sequence_create_page(request):
    blocked = _admin_required(request)
    if blocked:
        return blocked
    return render(
        request,
        "followups/sequence_create.html",
        {
            "api_accounts": _connected_accounts(request.crm_user, "api"),
            "hosted_accounts": _connected_accounts(request.crm_user, "hosted"),
        },
    )


@crm_login_required
@require_POST
def sequence_create_save(request):
    blocked = _admin_required(request)
    if blocked:
        return blocked
    user = request.crm_user
    provider = (request.POST.get("provider") or "api").strip().lower()
    if provider not in {"api", "hosted"}:
        messages.error(request, "Choose Use WhatsApp API or Use WhatsApp.")
        return redirect("followups-sequence-create")
    account = get_object_or_404(
        _connected_accounts(user, provider),
        id=request.POST.get("whatsapp_account", ""),
    )
    try:
        sequence = create_sequence(
            organization=user.organization,
            created_by=user,
            name=request.POST.get("name", ""),
            description=request.POST.get("description", ""),
            whatsapp_account=account,
        )
    except FollowupError as exc:
        messages.error(request, str(exc))
        return redirect("followups-sequence-create")

    label = "WhatsApp" if provider == "hosted" else "WhatsApp API"
    messages.success(request, f"{label} sequence created. Add the messages in delivery order.")
    return redirect("followups-sequence-edit", sequence_id=sequence.id)


@crm_login_required
@require_GET
def sequence_edit_page(request, sequence_id):
    blocked = _admin_required(request)
    if blocked:
        return blocked
    sequence = _organization_sequence(request, sequence_id)
    from apps.followups.views.web import _step_context

    context = _step_context(sequence)
    is_hosted = sequence.whatsapp_account.connection_type == "hosted"
    context.update(
        {
            "is_followup_admin": True,
            "is_hosted_sequence": is_hosted,
            "provider_label": "WhatsApp" if is_hosted else "WhatsApp API",
            "approved_template_count": 0,
        }
    )
    if not is_hosted:
        from apps.channels.models import WhatsAppTemplate

        context["approved_template_count"] = WhatsAppTemplate.objects.filter(
            organization=request.crm_user.organization,
            account=sequence.whatsapp_account,
            status=WhatsAppTemplate.Status.APPROVED,
        ).count()
    return render(request, "followups/sequence_edit.html", context)


@crm_login_required
@require_POST
def sequence_duplicate(request, sequence_id):
    blocked = _admin_required(request)
    if blocked:
        return blocked
    sequence = _organization_sequence(request, sequence_id)
    copied = duplicate_sequence(sequence=sequence, created_by=request.crm_user)
    duplicate_hosted_configs(source_sequence=sequence, copied_sequence=copied)
    messages.success(request, f"Duplicated as {copied.name}.")
    return redirect("followups-sequence-edit", sequence_id=copied.id)


@crm_login_required
@require_GET
def hosted_whatsapp_step_modal(request, sequence_id):
    blocked = _admin_required(request)
    if blocked:
        return blocked
    sequence = _organization_sequence(request, sequence_id)
    if sequence.whatsapp_account.connection_type != "hosted":
        raise Http404
    from apps.followups.views.web import _step_context

    return render(
        request,
        "followups/partials/hosted_whatsapp_step_modal.html",
        _step_context(sequence),
    )


@crm_login_required
@require_POST
def hosted_whatsapp_step_add(request, sequence_id):
    blocked = _admin_required(request)
    if blocked:
        return blocked
    sequence = _organization_sequence(request, sequence_id)
    if sequence.whatsapp_account.connection_type != "hosted":
        raise Http404
    from apps.followups.views.web import _schedule_payload

    try:
        add_hosted_whatsapp_step(
            sequence=sequence,
            title=request.POST.get("title", ""),
            body=request.POST.get("body", ""),
            attachment=request.FILES.get("attachment"),
            **_schedule_payload(request),
        )
    except (HostedAutomationError, FollowupError) as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "WhatsApp message added to the Hosted Account sequence.")
    return redirect("followups-sequence-edit", sequence_id=sequence.id)


@crm_login_required
@require_POST
def step_update(request, sequence_id, step_id):
    sequence = _organization_sequence(request, sequence_id)
    step = get_object_or_404(FollowupStep, id=step_id, sequence=sequence)
    if not (
        sequence.whatsapp_account.connection_type == "hosted"
        and step.step_type == FollowupStep.StepType.WHATSAPP
    ):
        from apps.followups.views.web import step_update as original_step_update

        return original_step_update(request, sequence_id, step_id)

    blocked = _admin_required(request)
    if blocked:
        return blocked
    from apps.followups.views.web import _schedule_payload
    from services.followup_service import _validate_schedule

    try:
        schedule = _schedule_payload(request)
        _validate_schedule(**schedule)
        step.schedule_type = schedule["schedule_type"]
        step.delay_value = schedule["delay_value"]
        step.delay_unit = schedule["delay_unit"]
        step.specific_time = schedule["specific_time"]
        step.specific_weekday = schedule["specific_weekday"]
        step.recurring_every = schedule["recurring_every"]
        step.recurring_unit = schedule["recurring_unit"]
        step.recurring_weekdays = schedule["recurring_weekdays"]
        step.save()
        update_hosted_whatsapp_step(
            step=step,
            title=request.POST.get("title", step.title),
            body=request.POST.get("body", ""),
            attachment=request.FILES.get("attachment"),
            remove_attachment=request.POST.get("remove_attachment") == "on",
        )
    except (HostedAutomationError, FollowupError, ValueError) as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Step {step.position} saved.")
    return redirect("followups-sequence-edit", sequence_id=sequence.id)


@crm_login_required
@require_http_methods(["GET", "POST"])
def hosted_account_health(request, account_id):
    if not is_hosted_account_enabled(request.crm_user.organization):
        raise Http404("Hosted Account is not enabled for this organization.")

    account = get_object_or_404(
        WhatsAppAccount,
        id=account_id,
        organization=request.crm_user.organization,
        connection_type="hosted",
        is_active=True,
    )
    if request.method == "POST":
        enabled = str(request.POST.get("enabled", "")).lower() in {"1", "true", "yes", "on"}
        snapshot = set_health_enabled(account=account, enabled=enabled)
    else:
        snapshot = health_snapshot(account=account)
    paused_until = snapshot.get("paused_until")
    snapshot["paused_until"] = paused_until.isoformat() if paused_until else None
    return JsonResponse({"ok": True, "health": snapshot})


@require_GET
def hosted_followup_media(request, config_id):
    """Short-lived signed internal media URL consumed by whatsapp-web.js."""
    token = request.GET.get("token", "")
    try:
        unsigned = TimestampSigner(salt="hosted-followup-media").unsign(
            token,
            max_age=MEDIA_TOKEN_MAX_AGE_SECONDS,
        )
    except (BadSignature, SignatureExpired):
        raise Http404
    if unsigned != str(config_id):
        raise Http404
    hosted = get_object_or_404(HostedFollowupStepConfig, id=config_id)
    if not hosted.attachment:
        raise Http404
    content_type = hosted.attachment_mime_type or mimetypes.guess_type(
        hosted.attachment_original_name
    )[0] or "application/octet-stream"
    response = FileResponse(
        hosted.attachment.open("rb"),
        content_type=content_type,
        as_attachment=False,
        filename=hosted.attachment_original_name or None,
    )
    response["Cache-Control"] = "private, max-age=60"
    return response
