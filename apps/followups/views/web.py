from datetime import datetime

from django.contrib import messages
from django.db.models import Count, Q
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.models import User
from apps.channels.models import WhatsAppAccount, WhatsAppTemplate
from apps.crm.decorators import crm_login_required
from apps.crm.models import Lead
from apps.followups.models import (
    AutoFollowupSettings,
    FollowupSequence,
    FollowupStep,
    LeadSequenceState,
)
from services.followup_service import (
    FollowupError,
    _validate_schedule,
    add_email_step,
    add_reminder_step,
    add_whatsapp_step,
    assign_sequence,
    clear_sequence,
    create_sequence,
    delete_sequence,
    delete_step,
    duplicate_sequence,
    get_auto_followup_settings,
    set_lead_followup_enabled,
    update_auto_followup_settings,
    update_sequence,
)


def _is_admin(user):
    return user.role == User.Role.ADMIN


def _admin_required(request):
    if not _is_admin(request.crm_user):
        return HttpResponseForbidden(
            "Only organization admins can manage Auto Follow-up sequences."
        )
    return None


def _organization_sequence(request, sequence_id):
    return get_object_or_404(
        FollowupSequence.objects.select_related("whatsapp_account", "created_by"),
        id=sequence_id,
        organization=request.crm_user.organization,
    )


def _connected_accounts(user):
    return WhatsAppAccount.objects.filter(
        organization=user.organization,
        status=WhatsAppAccount.Status.CONNECTED,
        is_active=True,
    ).order_by("business_name", "display_phone_number")


def _parse_time(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise FollowupError("Enter time in HH:MM format.") from exc


def _parse_optional_int(value, error_message):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise FollowupError(error_message) from exc


def _schedule_payload(request):
    schedule_type = request.POST.get(
        "schedule_type",
        FollowupStep.ScheduleType.IMMEDIATE,
    ).strip()

    payload = {
        "schedule_type": schedule_type,
        "delay_value": None,
        "delay_unit": "",
        "specific_time": None,
        "specific_weekday": None,
        "recurring_every": None,
        "recurring_unit": "",
        "recurring_weekdays": [],
    }

    if schedule_type == FollowupStep.ScheduleType.IMMEDIATE:
        return payload

    if schedule_type == FollowupStep.ScheduleType.DELAY:
        payload["delay_value"] = _parse_optional_int(
            request.POST.get("delay_value"),
            "Delay must be a whole number.",
        )
        payload["delay_unit"] = request.POST.get("delay_unit", "").strip()
        return payload

    if schedule_type == FollowupStep.ScheduleType.SPECIFIC_TIME:
        payload["specific_time"] = _parse_time(
            request.POST.get("specific_time", "").strip()
        )
        payload["specific_weekday"] = _parse_optional_int(
            request.POST.get("specific_weekday"),
            "Invalid weekday.",
        )
        return payload

    if schedule_type == FollowupStep.ScheduleType.RECURRING:
        recurring_mode = request.POST.get("recurring_mode", "specific_days").strip()
        if recurring_mode == "specific_days":
            recurring_weekdays = []
            for raw_day in request.POST.getlist("recurring_weekdays"):
                try:
                    day = int(raw_day)
                except ValueError as exc:
                    raise FollowupError("Invalid recurring weekday.") from exc
                if day not in recurring_weekdays:
                    recurring_weekdays.append(day)
            payload["recurring_weekdays"] = recurring_weekdays
            payload["specific_time"] = _parse_time(
                request.POST.get("recurring_time", "").strip()
            )
            return payload

        if recurring_mode == "interval":
            payload["recurring_every"] = _parse_optional_int(
                request.POST.get("recurring_every"),
                "Recurring interval must be a whole number.",
            )
            payload["recurring_unit"] = request.POST.get(
                "recurring_unit",
                "",
            ).strip()
            return payload

        raise FollowupError("Choose a valid recurring schedule type.")

    raise FollowupError("Choose a valid delivery schedule.")


def _step_context(sequence):
    steps = list(
        sequence.steps.select_related("whatsapp_template").order_by(
            "position",
            "created_at",
        )
    )
    return {
        "sequence": sequence,
        "steps": steps,
        "step_types": FollowupStep.StepType,
        "schedule_types": FollowupStep.ScheduleType,
        "delay_units": FollowupStep.DelayUnit,
        "weekdays": FollowupStep.Weekday,
    }


@crm_login_required
@require_GET
def sequence_list(request):
    user = request.crm_user
    search = request.GET.get("q", "").strip()
    sequences = (
        FollowupSequence.objects.filter(organization=user.organization)
        .select_related("whatsapp_account")
        .annotate(
            total_steps=Count("steps", distinct=True),
            whatsapp_steps=Count(
                "steps",
                filter=Q(steps__step_type=FollowupStep.StepType.WHATSAPP),
                distinct=True,
            ),
            email_steps=Count(
                "steps",
                filter=Q(steps__step_type=FollowupStep.StepType.EMAIL),
                distinct=True,
            ),
            reminder_steps=Count(
                "steps",
                filter=Q(steps__step_type=FollowupStep.StepType.REMINDER),
                distinct=True,
            ),
            assigned_leads=Count(
                "lead_states",
                filter=Q(
                    lead_states__status__in=[
                        LeadSequenceState.Status.ACTIVE,
                        LeadSequenceState.Status.PAUSED,
                    ]
                ),
                distinct=True,
            ),
        )
        .order_by("-updated_at")
    )
    if search:
        sequences = sequences.filter(
            Q(name__icontains=search) | Q(description__icontains=search)
        )

    return render(
        request,
        "followups/sequence_list.html",
        {
            "sequences": sequences,
            "search": search,
            "is_followup_admin": _is_admin(user),
            "followup_settings": get_auto_followup_settings(user.organization),
        },
    )


@crm_login_required
@require_GET
def sequence_create_page(request):
    blocked = _admin_required(request)
    if blocked:
        return blocked
    return render(
        request,
        "followups/sequence_create.html",
        {"accounts": _connected_accounts(request.crm_user)},
    )


@crm_login_required
@require_POST
def sequence_create_save(request):
    blocked = _admin_required(request)
    if blocked:
        return blocked
    user = request.crm_user
    account = get_object_or_404(
        _connected_accounts(user),
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
    messages.success(
        request,
        "Sequence created. Add WhatsApp, email, or reminder steps in any order.",
    )
    return redirect("followups-sequence-edit", sequence_id=sequence.id)


@crm_login_required
@require_GET
def sequence_edit_page(request, sequence_id):
    blocked = _admin_required(request)
    if blocked:
        return blocked
    sequence = _organization_sequence(request, sequence_id)
    context = _step_context(sequence)
    context.update(
        {
            "is_followup_admin": True,
            "approved_template_count": WhatsAppTemplate.objects.filter(
                organization=request.crm_user.organization,
                account=sequence.whatsapp_account,
                status=WhatsAppTemplate.Status.APPROVED,
            ).count(),
        }
    )
    return render(request, "followups/sequence_edit.html", context)


@crm_login_required
@require_POST
def sequence_update_save(request, sequence_id):
    blocked = _admin_required(request)
    if blocked:
        return blocked
    sequence = _organization_sequence(request, sequence_id)
    try:
        update_sequence(
            sequence=sequence,
            name=request.POST.get("name", ""),
            description=request.POST.get("description", ""),
        )
    except FollowupError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Sequence details saved.")
    return redirect("followups-sequence-edit", sequence_id=sequence.id)


@crm_login_required
@require_POST
def sequence_duplicate(request, sequence_id):
    blocked = _admin_required(request)
    if blocked:
        return blocked
    sequence = _organization_sequence(request, sequence_id)
    copied = duplicate_sequence(sequence=sequence, created_by=request.crm_user)
    messages.success(request, f"Duplicated as {copied.name}.")
    return redirect("followups-sequence-edit", sequence_id=copied.id)


@crm_login_required
@require_POST
def sequence_delete(request, sequence_id):
    blocked = _admin_required(request)
    if blocked:
        return blocked
    sequence = _organization_sequence(request, sequence_id)
    try:
        delete_sequence(sequence=sequence)
    except FollowupError as exc:
        messages.error(request, str(exc))
        return redirect("followups-sequence-edit", sequence_id=sequence.id)
    messages.success(request, "Sequence deleted.")
    return redirect("crm-auto-follow-ups-sequences")


@crm_login_required
@require_GET
def template_picker(request, sequence_id):
    blocked = _admin_required(request)
    if blocked:
        return blocked
    sequence = _organization_sequence(request, sequence_id)
    templates = (
        WhatsAppTemplate.objects.filter(
            organization=request.crm_user.organization,
            account=sequence.whatsapp_account,
            status=WhatsAppTemplate.Status.APPROVED,
        )
        .select_related("account", "meta_state")
        .order_by("category", "name")
    )
    return render(
        request,
        "followups/partials/template_picker.html",
        {"sequence": sequence, "templates": templates},
    )


@crm_login_required
@require_GET
def template_preview(request, sequence_id, template_id):
    blocked = _admin_required(request)
    if blocked:
        return blocked
    sequence = _organization_sequence(request, sequence_id)
    template = get_object_or_404(
        WhatsAppTemplate.objects.select_related("account"),
        id=template_id,
        organization=request.crm_user.organization,
        account=sequence.whatsapp_account,
        status=WhatsAppTemplate.Status.APPROVED,
    )
    return render(
        request,
        "followups/partials/template_preview.html",
        {"sequence": sequence, "template": template},
    )


@crm_login_required
@require_POST
def template_add(request, sequence_id):
    blocked = _admin_required(request)
    if blocked:
        return blocked
    sequence = _organization_sequence(request, sequence_id)
    template = get_object_or_404(
        WhatsAppTemplate,
        id=request.POST.get("template_id", ""),
        organization=request.crm_user.organization,
        account=sequence.whatsapp_account,
        status=WhatsAppTemplate.Status.APPROVED,
    )
    try:
        add_whatsapp_step(
            sequence=sequence,
            template=template,
            schedule_type=FollowupStep.ScheduleType.IMMEDIATE,
            retry_count=0,
        )
    except FollowupError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"{template.name} added to the sequence.")
    return redirect("followups-sequence-edit", sequence_id=sequence.id)


@crm_login_required
@require_GET
def email_step_modal(request, sequence_id):
    blocked = _admin_required(request)
    if blocked:
        return blocked
    sequence = _organization_sequence(request, sequence_id)
    return render(
        request,
        "followups/partials/email_step_modal.html",
        {**_step_context(sequence)},
    )


@crm_login_required
@require_POST
def email_step_add(request, sequence_id):
    blocked = _admin_required(request)
    if blocked:
        return blocked
    sequence = _organization_sequence(request, sequence_id)
    try:
        add_email_step(
            sequence=sequence,
            title=request.POST.get("title", ""),
            subject=request.POST.get("email_subject", ""),
            body=request.POST.get("email_body", ""),
            **_schedule_payload(request),
        )
    except FollowupError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            "Email follow-up added. Delivery remains gated until email "
            "DNS/sender configuration is enabled.",
        )
    return redirect("followups-sequence-edit", sequence_id=sequence.id)


@crm_login_required
@require_GET
def reminder_step_modal(request, sequence_id):
    blocked = _admin_required(request)
    if blocked:
        return blocked
    sequence = _organization_sequence(request, sequence_id)
    return render(
        request,
        "followups/partials/reminder_step_modal.html",
        {**_step_context(sequence)},
    )


@crm_login_required
@require_POST
def reminder_step_add(request, sequence_id):
    blocked = _admin_required(request)
    if blocked:
        return blocked
    sequence = _organization_sequence(request, sequence_id)
    try:
        add_reminder_step(
            sequence=sequence,
            text=request.POST.get("reminder_text", ""),
            **_schedule_payload(request),
        )
    except FollowupError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Call reminder added to the sequence.")
    return redirect("followups-sequence-edit", sequence_id=sequence.id)


@crm_login_required
@require_POST
def step_update(request, sequence_id, step_id):
    blocked = _admin_required(request)
    if blocked:
        return blocked
    sequence = _organization_sequence(request, sequence_id)
    step = get_object_or_404(FollowupStep, id=step_id, sequence=sequence)
    try:
        schedule = _schedule_payload(request)
        _validate_schedule(**schedule)
        retry_count = int(request.POST.get("retry_count", step.retry_count) or 0)
        if retry_count < 0 or retry_count > 5:
            raise FollowupError("Message Retry Count can be from 0 to 5.")

        step.schedule_type = schedule["schedule_type"]
        step.delay_value = schedule["delay_value"]
        step.delay_unit = schedule["delay_unit"]
        step.specific_time = schedule["specific_time"]
        step.specific_weekday = schedule["specific_weekday"]
        step.recurring_every = schedule["recurring_every"]
        step.recurring_unit = schedule["recurring_unit"]
        step.recurring_weekdays = schedule["recurring_weekdays"]

        if step.step_type == FollowupStep.StepType.WHATSAPP:
            step.retry_count = retry_count
            step.retry_delay_hours = 24
        elif step.step_type == FollowupStep.StepType.EMAIL:
            step.title = request.POST.get("title", step.title).strip()
            step.email_subject = request.POST.get(
                "email_subject",
                step.email_subject,
            ).strip()
            step.email_body = request.POST.get(
                "email_body",
                step.email_body,
            ).strip()
            if not step.email_subject or not step.email_body:
                raise FollowupError("Email subject and content are required.")
        elif step.step_type == FollowupStep.StepType.REMINDER:
            step.reminder_text = request.POST.get(
                "reminder_text",
                step.reminder_text,
            ).strip()
            if not step.reminder_text:
                raise FollowupError("Reminder note is required.")
        step.save()
    except (FollowupError, ValueError) as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Step {step.position} saved.")
    return redirect("followups-sequence-edit", sequence_id=sequence.id)


@crm_login_required
@require_POST
def step_delete_view(request, sequence_id, step_id):
    blocked = _admin_required(request)
    if blocked:
        return blocked
    sequence = _organization_sequence(request, sequence_id)
    step = get_object_or_404(FollowupStep, id=step_id, sequence=sequence)
    delete_step(step=step)
    messages.success(request, "Sequence step deleted.")
    return redirect("followups-sequence-edit", sequence_id=sequence.id)


@crm_login_required
@require_POST
def step_move(request, sequence_id, step_id):
    blocked = _admin_required(request)
    if blocked:
        return blocked
    sequence = _organization_sequence(request, sequence_id)
    step = get_object_or_404(FollowupStep, id=step_id, sequence=sequence)
    direction = request.POST.get("direction", "").strip()
    if direction not in {"up", "down"}:
        return HttpResponse("Invalid direction.", status=400)
    neighbor_qs = sequence.steps.exclude(id=step.id)
    if direction == "up":
        neighbor = neighbor_qs.filter(position__lt=step.position).order_by(
            "-position"
        ).first()
    else:
        neighbor = neighbor_qs.filter(position__gt=step.position).order_by(
            "position"
        ).first()
    if neighbor:
        old_position = step.position
        step.position = 0
        step.save(update_fields=["position", "updated_at"])
        neighbor_position = neighbor.position
        neighbor.position = old_position
        neighbor.save(update_fields=["position", "updated_at"])
        step.position = neighbor_position
        step.save(update_fields=["position", "updated_at"])
    return redirect("followups-sequence-edit", sequence_id=sequence.id)


@crm_login_required
@require_GET
def settings_modal(request):
    user = request.crm_user
    return render(
        request,
        "followups/partials/settings_modal.html",
        {
            "followup_settings": get_auto_followup_settings(user.organization),
            "delay_units": AutoFollowupSettings.DelayUnit,
            "is_followup_admin": _is_admin(user),
        },
    )


@crm_login_required
@require_POST
def settings_save(request):
    blocked = _admin_required(request)
    if blocked:
        return blocked
    try:
        delay_value = int(
            request.POST.get("conversation_delay_value", "2") or 0
        )
        update_auto_followup_settings(
            request.crm_user.organization,
            enabled=request.POST.get("enabled") == "on",
            business_hours_start=_parse_time(
                request.POST.get("business_hours_start", "")
            ),
            business_hours_end=_parse_time(
                request.POST.get("business_hours_end", "")
            ),
            conversation_delay_value=delay_value,
            conversation_delay_unit=request.POST.get(
                "conversation_delay_unit",
                AutoFollowupSettings.DelayUnit.HOURS,
            ),
        )
    except (FollowupError, ValueError, TypeError) as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Auto Follow-up settings saved.")
    return redirect("crm-auto-follow-ups-sequences")


@crm_login_required
@require_POST
def lead_assign_sequence(request, lead_id):
    user = request.crm_user
    lead = get_object_or_404(
        Lead,
        id=lead_id,
        organization=user.organization,
    )
    sequence = get_object_or_404(
        FollowupSequence,
        id=request.POST.get("sequence_id", ""),
        organization=user.organization,
        is_active=True,
    )
    try:
        assign_sequence(lead=lead, sequence=sequence, actor=user)
    except FollowupError as exc:
        return HttpResponse(str(exc), status=400)
    return redirect("followups-lead-control", lead_id=lead.id)


@crm_login_required
@require_POST
def lead_clear_sequence(request, lead_id):
    lead = get_object_or_404(
        Lead,
        id=lead_id,
        organization=request.crm_user.organization,
    )
    clear_sequence(lead=lead)
    return redirect("followups-lead-control", lead_id=lead.id)


@crm_login_required
@require_POST
def lead_toggle_sequence(request, lead_id):
    lead = get_object_or_404(
        Lead,
        id=lead_id,
        organization=request.crm_user.organization,
    )
    try:
        set_lead_followup_enabled(
            lead=lead,
            enabled=request.POST.get("enabled") == "true",
        )
    except FollowupError as exc:
        return HttpResponse(str(exc), status=400)
    return redirect("followups-lead-control", lead_id=lead.id)
