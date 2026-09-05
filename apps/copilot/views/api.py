import json

from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.accounts.models import User
from apps.copilot.models import CopilotLeadFlag
from apps.crm.decorators import crm_login_required
from apps.crm.models import Lead, Stage
from services.copilot_service import (
    FLAG_DEFINITIONS,
    active_flags_for_user,
    ensure_fresh_cache,
    flag_payload,
    get_copilot_config,
    refresh_organization_flags,
    resolve_flag,
    snooze_flag,
    update_copilot_config,
    visible_pipelines_for_user,
)
from services.crm_activity_service import record_stage_changed


def _json_body(request):
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Request body must be valid JSON.") from exc


def _flag_for_user_or_404(user, flag_id):
    return get_object_or_404(
        CopilotLeadFlag.objects.select_related(
            "lead",
            "lead__pipeline",
            "lead__stage",
        ),
        id=flag_id,
        organization=user.organization,
        lead__pipeline__in=visible_pipelines_for_user(user),
    )


@crm_login_required
@require_GET
def flags_api(request):
    user = request.crm_user
    config = get_copilot_config(user.organization)
    if not config["copilot_enabled"]:
        return JsonResponse(
            {
                "enabled": False,
                "count": 0,
                "results": [],
            }
        )

    ensure_fresh_cache(user.organization)
    flags = active_flags_for_user(
        user,
        pipeline_id=request.GET.get("pipeline_id") or None,
        severity=request.GET.get("severity") or None,
        flag_code=request.GET.get("flag") or None,
    )

    results = []
    for flag in flags:
        payload = flag_payload(flag)
        payload["lead"] = {
            "id": str(flag.lead.id),
            "name": flag.lead.name,
            "phone": flag.lead.phone,
            "pipeline": flag.lead.pipeline.name,
            "pipeline_id": str(flag.lead.pipeline_id),
            "stage": flag.lead.stage.name,
            "stage_id": str(flag.lead.stage_id),
            "quick_view_url": f"/dashboard/leads/{flag.lead.id}/",
            "send_message_url": f"/dashboard/whatsapp/chats/{flag.lead.id}/",
        }
        results.append(payload)

    return JsonResponse(
        {
            "enabled": True,
            "count": len(results),
            "results": results,
        }
    )


@crm_login_required
@require_POST
def snooze_api(request, flag_id):
    user = request.crm_user
    flag = _flag_for_user_or_404(user, flag_id)
    try:
        payload = _json_body(request)
        snooze_flag(flag, str(payload.get("duration", "")).strip())
    except ValueError as exc:
        return JsonResponse({"message": str(exc)}, status=400)

    return JsonResponse(
        {
            "message": "Flag snoozed.",
            "snoozed_until": flag.snoozed_until.isoformat(),
        }
    )


@crm_login_required
@require_POST
def resolve_api(request, flag_id):
    user = request.crm_user
    flag = _flag_for_user_or_404(user, flag_id)
    resolve_flag(flag)
    return JsonResponse({"message": "Flag marked reviewed."})


@crm_login_required
@require_http_methods(["GET", "PATCH"])
def config_api(request):
    user = request.crm_user
    organization = user.organization

    if request.method == "GET":
        return JsonResponse(get_copilot_config(organization))

    if user.role != User.Role.ADMIN:
        return JsonResponse(
            {"message": "Only organization admins can change Co-Pilot settings."},
            status=403,
        )

    try:
        payload = _json_body(request)
        config = update_copilot_config(organization, payload)
        # Apply the master switch and threshold changes immediately instead of
        # waiting for the next 30-minute scheduled refresh.
        refresh_organization_flags(organization)
    except ValueError as exc:
        return JsonResponse({"message": str(exc)}, status=400)

    return JsonResponse({"message": "Co-Pilot settings saved.", "config": config})


@crm_login_required
@require_POST
@transaction.atomic
def move_stage_api(request, lead_id):
    """Move a lead using the CRM's existing stage model and activity trail."""

    user = request.crm_user
    try:
        payload = _json_body(request)
    except ValueError as exc:
        return JsonResponse({"message": str(exc)}, status=400)

    stage_id = str(payload.get("stage_id", "")).strip()
    if not stage_id:
        return JsonResponse({"message": "stage_id is required."}, status=400)

    lead = get_object_or_404(
        Lead.objects.select_for_update().select_related("pipeline", "stage"),
        id=lead_id,
        organization=user.organization,
        pipeline__in=visible_pipelines_for_user(user),
    )
    new_stage = get_object_or_404(
        Stage,
        id=stage_id,
        pipeline=lead.pipeline,
        is_active=True,
    )

    if lead.stage_id == new_stage.id:
        return JsonResponse(
            {"message": "Lead is already in this stage.", "stage": new_stage.name}
        )

    old_stage = lead.stage
    lead.stage = new_stage
    lead.stage_entered_at = timezone.now()
    lead.save(update_fields=["stage", "stage_entered_at", "updated_at"])
    record_stage_changed(
        lead=lead,
        actor=user,
        pipeline=lead.pipeline,
        old_stage=old_stage,
        new_stage=new_stage,
    )

    # The stale-stage signal is now known to be invalid. Remove it immediately;
    # the scheduled scan will reconcile every other flag.
    CopilotLeadFlag.objects.filter(
        organization=user.organization,
        lead=lead,
        flag_code="X3",
    ).delete()

    return JsonResponse(
        {
            "message": "Lead moved successfully.",
            "stage": new_stage.name,
            "stage_id": str(new_stage.id),
        }
    )
