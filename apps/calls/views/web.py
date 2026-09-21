from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count, Q, Sum
from django.http import FileResponse, Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.models import User
from apps.crm.authentication import crm_login_required
from apps.crm.models import Pipeline, Stage

from ..models import CallDevice, CallRecord
from ..services import get_call_settings, visible_calls, visible_pipelines


ACTIVE_DEVICE_WINDOW_MINUTES = 15


def _can_manage(user):
    return user.role == User.Role.ADMIN


def _apk_state():
    configured_path = Path(
        str(getattr(settings, "CALL_INTELLIGENCE_APK_PATH", "") or "")
    )
    external_url = str(
        getattr(settings, "CALL_INTELLIGENCE_APK_URL", "") or ""
    ).strip()

    local_available = bool(
        str(configured_path)
        and configured_path.is_file()
        and configured_path.suffix.lower() == ".apk"
    )
    return {
        "path": configured_path,
        "url": external_url,
        "local_available": local_available,
        "available": local_available or bool(external_url),
        "version": str(
            getattr(settings, "CALL_INTELLIGENCE_ANDROID_VERSION", "1.0.0")
        ),
    }


@crm_login_required
def call_intelligence_dashboard(request):
    user = request.crm_user
    organization = user.organization
    calls = visible_calls(user)
    now = timezone.now()
    today = timezone.localdate()

    today_calls = calls.filter(called_at__date=today)
    metrics = today_calls.aggregate(
        total=Count("id"),
        inbound=Count(
            "id",
            filter=Q(direction=CallRecord.Direction.INBOUND),
        ),
        outbound=Count(
            "id",
            filter=Q(direction=CallRecord.Direction.OUTBOUND),
        ),
        completed=Count(
            "id",
            filter=Q(status=CallRecord.Status.COMPLETED),
        ),
        missed=Count(
            "id",
            filter=Q(status=CallRecord.Status.MISSED),
        ),
        seconds=Sum("duration_seconds"),
    )
    duration_seconds = int(metrics.pop("seconds") or 0)
    metrics["minutes"] = round(duration_seconds / 60, 1)

    device_qs = CallDevice.objects.filter(
        organization=organization,
        is_active=True,
    ).select_related("user")
    if user.role == User.Role.AGENT:
        device_qs = device_qs.filter(user=user)

    active_after = now - timedelta(minutes=ACTIVE_DEVICE_WINDOW_MINUTES)
    devices = list(device_qs.order_by("-last_seen_at")[:12])
    active_devices = sum(
        1
        for device in devices
        if device.last_seen_at and device.last_seen_at >= active_after
    )

    call_settings = get_call_settings(organization)
    pipelines = visible_pipelines(user).prefetch_related("stages").order_by("name")
    pipeline_ids = list(pipelines.values_list("id", flat=True))
    stages = Stage.objects.filter(
        pipeline_id__in=pipeline_ids,
        is_active=True,
    ).select_related("pipeline").order_by(
        "pipeline__name",
        "display_order",
        "name",
    )

    response = render(
        request,
        "calls/dashboard.html",
        {
            "crm_user": user,
            "call_settings": call_settings,
            "call_intelligence_admin": _can_manage(user),
            "metrics": metrics,
            "recent_calls": calls[:16],
            "devices": devices,
            "active_devices": active_devices,
            "device_count": device_qs.count(),
            "pipelines": pipelines,
            "stages": stages,
            "apk": _apk_state(),
            "today": today,
        },
    )
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@crm_login_required
def call_intelligence_download(request):
    apk = _apk_state()
    if apk["local_available"]:
        filename = f"SHVYA-Call-Intelligence-{apk['version']}.apk"
        return FileResponse(
            open(apk["path"], "rb"),
            as_attachment=True,
            filename=filename,
            content_type="application/vnd.android.package-archive",
        )

    if apk["url"]:
        return redirect(apk["url"])

    raise Http404(
        "The Android package has not been installed on this SHVYA deployment yet."
    )


@crm_login_required
@require_POST
def call_intelligence_settings(request):
    user = request.crm_user
    if not _can_manage(user):
        messages.error(request, "Only an organization admin can change Call Intelligence settings.")
        return redirect("call-intelligence-dashboard")

    call_settings = get_call_settings(user.organization)
    allowed_pipelines = visible_pipelines(user)

    pipeline = None
    stage = None
    pipeline_id = str(request.POST.get("default_pipeline") or "").strip()
    stage_id = str(request.POST.get("default_stage") or "").strip()

    if pipeline_id:
        pipeline = allowed_pipelines.filter(pk=pipeline_id).first()
        if pipeline is None:
            messages.error(request, "The selected pipeline is not available.")
            return redirect("call-intelligence-dashboard")

    if stage_id:
        if pipeline is None:
            messages.error(request, "Select a pipeline before choosing a stage.")
            return redirect("call-intelligence-dashboard")
        stage = Stage.objects.filter(
            pk=stage_id,
            pipeline=pipeline,
            is_active=True,
        ).first()
        if stage is None:
            messages.error(request, "The selected stage does not belong to that pipeline.")
            return redirect("call-intelligence-dashboard")

    country_code = str(request.POST.get("default_country_code") or "+91").strip()
    if not country_code.startswith("+") or not country_code[1:].isdigit():
        messages.error(request, "Country code must look like +91.")
        return redirect("call-intelligence-dashboard")

    call_settings.is_enabled = request.POST.get("is_enabled") == "on"
    call_settings.auto_create_answered = (
        request.POST.get("auto_create_answered") == "on"
    )
    call_settings.auto_create_missed = (
        request.POST.get("auto_create_missed") == "on"
    )
    call_settings.auto_create_outbound = (
        request.POST.get("auto_create_outbound") == "on"
    )
    call_settings.default_country_code = country_code[:8]
    call_settings.default_pipeline = pipeline
    call_settings.default_stage = stage

    try:
        call_settings.full_clean()
        call_settings.save()
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("call-intelligence-dashboard")

    messages.success(request, "Call Intelligence settings updated.")
    return redirect("call-intelligence-dashboard")
