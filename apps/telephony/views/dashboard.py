from datetime import timedelta

from django.conf import settings as django_settings
from django.db import transaction
from django.db.models import Avg, Count, Q
from django.http import Http404, HttpResponseRedirect, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from apps.accounts.models import User
from apps.crm.decorators import crm_login_required
from apps.crm.models import LeadReminder, Pipeline, Stage
from services.crm_activity_service import record_reminder_created

from ..models import (
    CallAppRelease,
    CallDevice,
    CallDisposition,
    CallIntelligenceResult,
    CallIntelligenceSettings,
    CallRecord,
)
from ..services import get_call_dispositions
from ..tasks import analyze_call_intelligence


def _scoped_calls(user):
    qs = CallRecord.objects.filter(organization=user.organization)
    if user.role == User.Role.AGENT:
        qs = qs.filter(user=user)
    return qs


def _filtered_calls(request, user):
    qs = _scoped_calls(user)
    status = str(request.GET.get("status") or "").strip()
    direction = str(request.GET.get("direction") or "").strip()
    source = str(request.GET.get("source") or "").strip()
    intent = str(request.GET.get("intent") or "").strip()
    pipeline = str(request.GET.get("pipeline") or "").strip()
    agent = str(request.GET.get("agent") or "").strip()
    query = str(request.GET.get("q") or "").strip()
    date_from = parse_date(str(request.GET.get("date_from") or ""))
    date_to = parse_date(str(request.GET.get("date_to") or ""))

    if status in CallRecord.Status.values:
        qs = qs.filter(status=status)
    if direction in CallRecord.Direction.values:
        qs = qs.filter(direction=direction)
    if source in CallRecord.Source.values:
        qs = qs.filter(source=source)
    if intent in CallIntelligenceResult.Intent.values:
        qs = qs.filter(intelligence__intent=intent)
    if pipeline and Pipeline.objects.filter(
        id=pipeline, organization=user.organization
    ).exists():
        qs = qs.filter(lead__pipeline_id=pipeline)
    if (
        agent
        and user.role != User.Role.AGENT
        and User.objects.filter(id=agent, organization=user.organization).exists()
    ):
        qs = qs.filter(user_id=agent)
    if query:
        qs = qs.filter(
            Q(phone_number__icontains=query)
            | Q(contact_name__icontains=query)
            | Q(lead__name__icontains=query)
        )
    if date_from:
        qs = qs.filter(ended_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(ended_at__date__lte=date_to)
    return qs


@crm_login_required
def call_intelligence_dashboard(request):
    user = request.crm_user
    settings_obj, _ = CallIntelligenceSettings.objects.get_or_create(
        organization=user.organization
    )
    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    scoped = _scoped_calls(user)
    calls = _filtered_calls(request, user)
    today = scoped.filter(ended_at__gte=start)
    stats = today.aggregate(total=Count("id"), average_talk=Avg("talk_duration_seconds"))

    recent_calls = list(
        calls.select_related(
            "lead", "lead__pipeline", "lead__stage", "user", "intelligence"
        ).order_by("-ended_at", "-created_at")[:50]
    )
    missed_calls = list(
        scoped.filter(status=CallRecord.Status.MISSED)
        .select_related("lead", "user")
        .order_by("-ended_at", "-created_at")[:12]
    )
    devices = list(
        CallDevice.objects.filter(organization=user.organization, is_active=True)
        .select_related("user")
        .order_by("-last_seen_at", "user__name")
    )
    agent_stats = list(
        today.exclude(user_id__isnull=True)
        .values("user_id", "user__name", "user__email")
        .annotate(
            calls=Count("id"),
            answered=Count("id", filter=Q(status=CallRecord.Status.ANSWERED)),
            missed=Count("id", filter=Q(status=CallRecord.Status.MISSED)),
            average_talk=Avg("talk_duration_seconds"),
            high_intent=Count(
                "id",
                filter=Q(intelligence__intent=CallIntelligenceResult.Intent.HIGH),
            ),
        )
        .order_by("-calls", "user__name")
    )
    release = CallAppRelease.objects.filter(is_active=True).first()
    release_url = release.download_url if release else getattr(
        django_settings, "CALL_INTELLIGENCE_APK_URL", ""
    )
    pipelines = Pipeline.objects.filter(
        organization=user.organization, is_active=True
    ).order_by("name")
    owners = User.objects.filter(
        organization=user.organization, is_active=True
    ).order_by("name", "email")

    return render(request, "telephony/call_intelligence.html", {
        "crm_user": user,
        "call_settings": settings_obj,
        "recent_calls": recent_calls,
        "missed_calls": missed_calls,
        "devices": devices,
        "agent_stats": agent_stats,
        "dispositions": list(get_call_dispositions(user.organization)),
        "pipelines": pipelines,
        "stages": Stage.objects.filter(
            pipeline__organization=user.organization,
            pipeline__is_active=True,
            is_active=True,
        ).select_related("pipeline").order_by("pipeline__name", "display_order"),
        "owners": owners,
        "stats": {
            "total": stats["total"] or 0,
            "answered": today.filter(status=CallRecord.Status.ANSWERED).count(),
            "missed": today.filter(status=CallRecord.Status.MISSED).count(),
            "outgoing": today.filter(direction=CallRecord.Direction.OUTGOING).count(),
            "average_talk": int(stats["average_talk"] or 0),
            "followups": scoped.filter(
                follow_up_required=True,
                follow_up_at__isnull=False,
                follow_up_at__lte=timezone.now() + timedelta(days=1),
            ).count(),
        },
        "filters": {
            key: str(request.GET.get(key) or "")
            for key in (
                "q", "status", "direction", "source", "intent",
                "pipeline", "agent", "date_from", "date_to",
            )
        },
        "release": release,
        "release_url": release_url,
        "is_admin": user.role == User.Role.ADMIN,
        "call_statuses": CallRecord.Status.choices,
        "call_directions": CallRecord.Direction.choices,
        "call_sources": CallRecord.Source.choices,
        "call_intents": CallIntelligenceResult.Intent.choices,
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
def post_call_action(request, call_id):
    user = request.crm_user
    call = (
        _scoped_calls(user)
        .select_related("lead", "crm_call")
        .filter(pk=call_id)
        .first()
    )
    if call is None:
        raise Http404("Call not found.")

    notes = str(request.POST.get("notes") or "").strip()[:20000]
    disposition = str(request.POST.get("disposition") or "").strip()[:80]
    if disposition and not get_call_dispositions(user.organization).filter(
        code=disposition
    ).exists():
        return JsonResponse({"ok": False, "error": "Invalid disposition."}, status=400)

    due_raw = str(request.POST.get("follow_up_at") or "").strip()
    due_at = parse_datetime(due_raw) if due_raw else None
    if due_raw and due_at is None:
        return JsonResponse(
            {"ok": False, "error": "Invalid follow-up date/time."},
            status=400,
        )
    if due_at and timezone.is_naive(due_at):
        due_at = timezone.make_aware(due_at, timezone.get_current_timezone())

    with transaction.atomic():
        call.notes = notes
        call.disposition = disposition
        call.save(update_fields=["notes", "disposition", "updated_at"])
        if call.crm_call_id:
            call.crm_call.notes = notes
            call.crm_call.save(update_fields=["notes"])

        if due_at:
            if not call.lead_id:
                return JsonResponse(
                    {"ok": False, "error": "A CRM lead is required for a reminder."},
                    status=400,
                )
            reminder = LeadReminder.objects.create(
                lead=call.lead,
                assigned_to=user,
                title="Call follow-up",
                description=notes[:2000],
                due_at=due_at,
                status="pending",
            )
            record_reminder_created(lead=call.lead, actor=user, reminder=reminder)
            call.follow_up_required = True
            call.follow_up_at = due_at
            call.save(update_fields=["follow_up_required", "follow_up_at", "updated_at"])

    if notes or call.transcript:
        transaction.on_commit(lambda: analyze_call_intelligence.delay(str(call.id)))

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"ok": True})
    return HttpResponseRedirect(reverse("call-intelligence-dashboard") + "#calls")


@crm_login_required
@require_POST
def save_call_disposition(request):
    user = request.crm_user
    if user.role != User.Role.ADMIN:
        return JsonResponse({"ok": False, "error": "Admin access required."}, status=403)

    name = str(request.POST.get("name") or "").strip()[:120]
    code = slugify(str(request.POST.get("code") or name))[:80]
    category = str(request.POST.get("category") or "").strip()
    if not name or not code:
        return JsonResponse({"ok": False, "error": "Disposition name is required."}, status=400)
    if category not in CallDisposition.Category.values:
        return JsonResponse({"ok": False, "error": "Invalid disposition category."}, status=400)

    try:
        position = max(0, int(request.POST.get("position") or 0))
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "error": "Invalid position."}, status=400)

    row, _ = CallDisposition.objects.update_or_create(
        organization=user.organization,
        code=code,
        defaults={
            "name": name,
            "category": category,
            "position": position,
            "is_active": request.POST.get("is_active") in {"1", "true", "on", "yes"},
        },
    )
    return JsonResponse({"ok": True, "code": row.code})


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
    settings_obj.auto_create_unknown = checked("auto_create_unknown")
    settings_obj.default_pipeline = pipeline
    settings_obj.default_stage = stage
    settings_obj.default_owner = owner
    settings_obj.full_clean()
    settings_obj.save()
    return JsonResponse({"ok": True})
