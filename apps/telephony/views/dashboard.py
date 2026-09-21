from datetime import timedelta

from django.conf import settings as django_settings
from django.db.models import Avg, Count
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.models import User
from apps.crm.decorators import crm_login_required
from apps.crm.models import Pipeline, Stage

from ..models import CallAppRelease, CallDevice, CallIntelligenceSettings, CallRecord


def _scoped_calls(user):
    qs = CallRecord.objects.filter(organization=user.organization)
    if user.role == User.Role.AGENT:
        qs = qs.filter(user=user)
    return qs


@crm_login_required
def call_intelligence_dashboard(request):
    user = request.crm_user
    settings_obj, _ = CallIntelligenceSettings.objects.get_or_create(
        organization=user.organization
    )
    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    calls = _scoped_calls(user)
    today = calls.filter(ended_at__gte=start)
    stats = today.aggregate(total=Count("id"), average_talk=Avg("talk_duration_seconds"))

    recent_calls = list(
        calls.select_related(
            "lead", "lead__pipeline", "lead__stage", "user", "intelligence"
        ).order_by("-ended_at", "-created_at")[:25]
    )
    devices = list(
        CallDevice.objects.filter(organization=user.organization, is_active=True)
        .select_related("user")
        .order_by("-last_seen_at", "user__name")
    )
    release = CallAppRelease.objects.filter(is_active=True).first()
    release_url = release.download_url if release else getattr(
        django_settings, "CALL_INTELLIGENCE_APK_URL", ""
    )

    return render(request, "telephony/call_intelligence.html", {
        "crm_user": user,
        "call_settings": settings_obj,
        "recent_calls": recent_calls,
        "devices": devices,
        "pipelines": Pipeline.objects.filter(
            organization=user.organization, is_active=True
        ).order_by("name"),
        "stages": Stage.objects.filter(
            pipeline__organization=user.organization,
            pipeline__is_active=True,
            is_active=True,
        ).select_related("pipeline").order_by("pipeline__name", "display_order"),
        "owners": User.objects.filter(
            organization=user.organization, is_active=True
        ).order_by("name", "email"),
        "stats": {
            "total": stats["total"] or 0,
            "answered": today.filter(status=CallRecord.Status.ANSWERED).count(),
            "missed": today.filter(status=CallRecord.Status.MISSED).count(),
            "outgoing": today.filter(direction=CallRecord.Direction.OUTGOING).count(),
            "average_talk": int(stats["average_talk"] or 0),
            "followups": calls.filter(
                follow_up_required=True,
                follow_up_at__isnull=False,
                follow_up_at__lte=timezone.now() + timedelta(days=1),
            ).count(),
        },
        "release": release,
        "release_url": release_url,
        "is_admin": user.role == User.Role.ADMIN,
    })


@crm_login_required
def download_android_app(request):
    release = CallAppRelease.objects.filter(is_active=True).first()
    url = release.download_url if release else getattr(
        django_settings, "CALL_INTELLIGENCE_APK_URL", ""
    )
    if not url:
        raise Http404("No Android release is currently published.")
    return redirect(url)


@crm_login_required
@require_POST
def update_call_settings(request):
    user = request.crm_user
    if user.role != User.Role.ADMIN:
        return JsonResponse({
            "ok": False,
            "error": "Only organization admins can change Call Intelligence settings.",
        }, status=403)

    settings_obj, _ = CallIntelligenceSettings.objects.get_or_create(
        organization=user.organization
    )
    pipeline_id = str(request.POST.get("default_pipeline") or "").strip()
    pipeline = (
        Pipeline.objects.filter(
            id=pipeline_id, organization=user.organization, is_active=True
        ).first()
        if pipeline_id else None
    )
    if pipeline_id and pipeline is None:
        return JsonResponse({"ok": False, "error": "Invalid pipeline."}, status=400)

    stage_id = str(request.POST.get("default_stage") or "").strip()
    stage = (
        Stage.objects.filter(id=stage_id, pipeline=pipeline, is_active=True).first()
        if stage_id else None
    )
    if stage_id and stage is None:
        return JsonResponse({"ok": False, "error": "Invalid stage."}, status=400)

    owner_id = str(request.POST.get("default_owner") or "").strip()
    owner = (
        User.objects.filter(
            id=owner_id, organization=user.organization, is_active=True
        ).first()
        if owner_id else None
    )
    if owner_id and owner is None:
        return JsonResponse({"ok": False, "error": "Invalid owner."}, status=400)

    def checked(name):
        return request.POST.get(name) in {"1", "true", "on", "yes"}
    settings_obj.enabled = checked("enabled")
    settings_obj.auto_create_answered_incoming = checked("auto_create_answered_incoming")
    settings_obj.auto_create_answered_outgoing = checked("auto_create_answered_outgoing")
    settings_obj.auto_create_missed = checked("auto_create_missed")
    settings_obj.auto_create_rejected = checked("auto_create_rejected")
    settings_obj.default_pipeline = pipeline
    settings_obj.default_stage = stage
    settings_obj.default_owner = owner
    settings_obj.full_clean()
    settings_obj.save()
    return JsonResponse({"ok": True})
