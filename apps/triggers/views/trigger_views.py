import json
import re
from json import JSONDecodeError

from django.contrib import messages
from django.db.models import Q, Sum
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.models import User
from apps.crm.decorators import crm_login_required
from apps.crm.models import Lead, Stage
from apps.followups.models import FollowupSequence
from apps.triggers.models import SmartTrigger
from services.triggers.evaluator import TriggerConfigurationError
from services.triggers.trigger_service import (
    create_trigger,
    duplicate_trigger,
    update_trigger,
)


_SAFE_ATTRIBUTE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")


def _is_admin(user):
    return user.role == User.Role.ADMIN


def _admin_required(request):
    if not _is_admin(request.crm_user):
        return HttpResponseForbidden(
            "Only organization admins can manage Smart Triggers."
        )
    return None


def _organization_trigger(request, trigger_id):
    return get_object_or_404(
        SmartTrigger.objects.select_related("created_by", "organization"),
        id=trigger_id,
        organization=request.crm_user.organization,
    )


def _decode_json_list(raw, label):
    try:
        value = json.loads(raw or "[]")
    except JSONDecodeError as exc:
        raise TriggerConfigurationError(f"{label} configuration is invalid.") from exc
    if not isinstance(value, list):
        raise TriggerConfigurationError(f"{label} configuration must be a list.")
    return value


def _post_values(request):
    return {
        "name": request.POST.get("name", ""),
        "description": request.POST.get("description", ""),
        "event_type": request.POST.get("event_type", ""),
        "condition_mode": request.POST.get(
            "condition_mode",
            SmartTrigger.ConditionMode.ALL,
        ),
        "conditions": _decode_json_list(
            request.POST.get("conditions_json", "[]"),
            "Condition",
        ),
        "actions": _decode_json_list(
            request.POST.get("actions_json", "[]"),
            "Action",
        ),
        "is_active": request.POST.get("is_active") == "on",
        "once_per_lead": request.POST.get("once_per_lead") == "on",
        "cooldown_minutes": request.POST.get("cooldown_minutes", "0"),
    }


def _attribute_fields(organization):
    keys = set()
    for attributes in Lead.objects.filter(organization=organization).values_list(
        "attributes",
        flat=True,
    )[:500]:
        for key in (attributes or {}).keys():
            key = str(key)
            if _SAFE_ATTRIBUTE_RE.match(key):
                keys.add(key)
    return sorted(keys, key=str.casefold)


def _builder_context(request, trigger=None, submitted=None):
    organization = request.crm_user.organization
    stages = list(
        Stage.objects.filter(
            pipeline__organization=organization,
            is_active=True,
        )
        .select_related("pipeline")
        .order_by("pipeline__name", "display_order", "name")
    )
    sequences = list(
        FollowupSequence.objects.filter(
            organization=organization,
            is_active=True,
        ).order_by("name")
    )

    if submitted is not None:
        initial = dict(submitted)
    elif trigger is not None:
        initial = {
            "name": trigger.name,
            "description": trigger.description,
            "event_type": trigger.event_type,
            "condition_mode": trigger.condition_mode,
            "conditions": list(trigger.conditions or []),
            "actions": list(trigger.actions or []),
            "is_active": trigger.is_active,
            "once_per_lead": trigger.once_per_lead,
            "cooldown_minutes": trigger.cooldown_minutes,
        }
    else:
        initial = {
            "name": "",
            "description": "",
            "event_type": SmartTrigger.EventType.LEAD_CREATED,
            "condition_mode": SmartTrigger.ConditionMode.ALL,
            "conditions": [],
            "actions": [],
            "is_active": True,
            "once_per_lead": False,
            "cooldown_minutes": 0,
        }

    field_options = [
        {"value": "lead.name", "label": "Lead name"},
        {"value": "lead.phone", "label": "Phone"},
        {"value": "lead.email", "label": "Email"},
        {"value": "lead.lead_source", "label": "Lead source"},
        {"value": "lead.pipeline_name", "label": "Pipeline name"},
        {"value": "lead.stage_name", "label": "Stage name"},
        {"value": "lead.notes", "label": "Lead notes"},
        {"value": "lead.ai_enabled", "label": "AI enabled"},
        {"value": "event.message_body", "label": "Incoming WhatsApp message"},
        {"value": "event.changed_fields", "label": "Changed fields"},
    ]
    field_options.extend(
        {"value": f"attr.{key}", "label": f"Custom: {key}"}
        for key in _attribute_fields(organization)
    )

    action_options = {
        "sequences": [
            {"id": str(sequence.id), "label": sequence.name}
            for sequence in sequences
        ],
        "stages": [
            {
                "id": str(stage.id),
                "label": f"{stage.pipeline.name} / {stage.name}",
            }
            for stage in stages
        ],
    }

    recent_executions = []
    if trigger is not None:
        recent_executions = list(
            trigger.executions.select_related("lead").order_by("-created_at")[:50]
        )

    return {
        "trigger": trigger,
        "initial": initial,
        "event_types": SmartTrigger.EventType.choices,
        "condition_modes": SmartTrigger.ConditionMode.choices,
        "field_options": field_options,
        "action_options": action_options,
        "recent_executions": recent_executions,
        # The rule-builder JavaScript contains examples such as {{lead_name}}.
        # Supplying these template variables preserves the literal token text
        # instead of letting Django render an undefined variable as an empty string.
        "lead_name": "{{lead_name}}",
        "phone": "{{phone}}",
        "email": "{{email}}",
        "org_name": "{{org_name}}",
        "city": "{{city}}",
    }


@crm_login_required
@require_GET
def trigger_list(request):
    organization = request.crm_user.organization
    search = request.GET.get("q", "").strip()
    status = request.GET.get("status", "all").strip()
    event_type = request.GET.get("event", "").strip()

    triggers = SmartTrigger.objects.filter(organization=organization).select_related(
        "created_by"
    )
    if search:
        triggers = triggers.filter(
            Q(name__icontains=search) | Q(description__icontains=search)
        )
    if status == "active":
        triggers = triggers.filter(is_active=True)
    elif status == "inactive":
        triggers = triggers.filter(is_active=False)
    if event_type in SmartTrigger.EventType.values:
        triggers = triggers.filter(event_type=event_type)

    all_triggers = SmartTrigger.objects.filter(organization=organization)
    stats = all_triggers.aggregate(total_runs=Sum("successful_runs"))
    stats.update(
        {
            "total": all_triggers.count(),
            "active": all_triggers.filter(is_active=True).count(),
            "successful_runs": stats.get("total_runs") or 0,
            "failed_runs": all_triggers.aggregate(total=Sum("failed_runs"))["total"]
            or 0,
        }
    )

    return render(
        request,
        "triggers/trigger_list.html",
        {
            "triggers": triggers.order_by("-updated_at"),
            "search": search,
            "status_filter": status,
            "event_filter": event_type,
            "event_types": SmartTrigger.EventType.choices,
            "is_trigger_admin": _is_admin(request.crm_user),
            "stats": stats,
        },
    )


@crm_login_required
@require_GET
def trigger_create_page(request):
    blocked = _admin_required(request)
    if blocked:
        return blocked
    return render(
        request,
        "triggers/trigger_form.html",
        _builder_context(request),
    )


@crm_login_required
@require_POST
def trigger_create_save(request):
    blocked = _admin_required(request)
    if blocked:
        return blocked
    try:
        values = _post_values(request)
        trigger = create_trigger(
            organization=request.crm_user.organization,
            created_by=request.crm_user,
            **values,
        )
    except TriggerConfigurationError as exc:
        messages.error(request, str(exc))
        submitted = locals().get("values")
        if submitted is None:
            submitted = {
                "name": request.POST.get("name", ""),
                "description": request.POST.get("description", ""),
                "event_type": request.POST.get("event_type", ""),
                "condition_mode": request.POST.get("condition_mode", "all"),
                "conditions": [],
                "actions": [],
                "is_active": request.POST.get("is_active") == "on",
                "once_per_lead": request.POST.get("once_per_lead") == "on",
                "cooldown_minutes": request.POST.get("cooldown_minutes", "0"),
            }
        return render(
            request,
            "triggers/trigger_form.html",
            _builder_context(request, submitted=submitted),
            status=400,
        )

    messages.success(request, "Smart Trigger created.")
    return redirect("smart-trigger-edit", trigger_id=trigger.id)


@crm_login_required
@require_GET
def trigger_edit_page(request, trigger_id):
    blocked = _admin_required(request)
    if blocked:
        return blocked
    trigger = _organization_trigger(request, trigger_id)
    return render(
        request,
        "triggers/trigger_form.html",
        _builder_context(request, trigger=trigger),
    )


@crm_login_required
@require_POST
def trigger_update_save(request, trigger_id):
    blocked = _admin_required(request)
    if blocked:
        return blocked
    trigger = _organization_trigger(request, trigger_id)
    try:
        values = _post_values(request)
        update_trigger(trigger=trigger, **values)
    except TriggerConfigurationError as exc:
        messages.error(request, str(exc))
        submitted = locals().get("values")
        if submitted is None:
            submitted = _builder_context(request, trigger=trigger)["initial"]
        return render(
            request,
            "triggers/trigger_form.html",
            _builder_context(request, trigger=trigger, submitted=submitted),
            status=400,
        )

    messages.success(request, "Smart Trigger saved.")
    return redirect("smart-trigger-edit", trigger_id=trigger.id)


@crm_login_required
@require_POST
def trigger_toggle(request, trigger_id):
    blocked = _admin_required(request)
    if blocked:
        return blocked
    trigger = _organization_trigger(request, trigger_id)
    trigger.is_active = not trigger.is_active
    trigger.save(update_fields=["is_active", "updated_at"])
    messages.success(
        request,
        f"{trigger.name} {'enabled' if trigger.is_active else 'paused'}.",
    )
    return redirect(request.POST.get("next") or "crm-smart-triggers")


@crm_login_required
@require_POST
def trigger_duplicate(request, trigger_id):
    blocked = _admin_required(request)
    if blocked:
        return blocked
    trigger = _organization_trigger(request, trigger_id)
    copied = duplicate_trigger(trigger=trigger, created_by=request.crm_user)
    messages.success(request, f"Duplicated as {copied.name}. Review it before enabling.")
    return redirect("smart-trigger-edit", trigger_id=copied.id)


@crm_login_required
@require_POST
def trigger_delete(request, trigger_id):
    blocked = _admin_required(request)
    if blocked:
        return blocked
    trigger = _organization_trigger(request, trigger_id)
    name = trigger.name
    trigger.delete()
    messages.success(request, f"{name} deleted.")
    return redirect("crm-smart-triggers")
