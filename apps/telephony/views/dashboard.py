
from django.conf import settings as django_settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.core.paginator import Paginator
from django.http import Http404, HttpResponseRedirect, JsonResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.utils.text import slugify
from urllib.parse import urlencode
from uuid import UUID
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.models import User
from apps.crm.decorators import crm_login_required
from apps.crm.models import LeadReminder, Pipeline, Stage
from apps.crm.views.api import get_user_pipelines
from services.crm_activity_service import record_reminder_created

from ..models import (
    CallAppRelease,
    CallDevice,
    CallDisposition,
    CallIntelligenceResult,
    CallIntelligenceSettings,
    CallRecord,
)
from ..services import create_crm_lead_from_call, get_call_dispositions, reconcile_call_tracking, request_call_analysis
from ..analytics import call_metrics, call_outcome_groups, team_metrics


def _scoped_calls(user):
    qs = CallRecord.objects.filter(organization=user.organization)
    if user.role == User.Role.AGENT:
        qs = qs.filter(user=user)
    return qs


def _filtered_calls(request, user, *, ignore=()):
    qs = _scoped_calls(user)
    params = request.GET.copy()
    for key in ignore:
        params.pop(key, None)
    status = str(params.get("status") or "").strip()
    disposition = str(params.get("disposition") or "").strip()
    direction = str(params.get("direction") or "").strip()
    source = str(params.get("source") or "").strip()
    intent = str(params.get("intent") or "").strip()
    pipeline = str(params.get("pipeline") or "").strip()
    agent = str(params.get("agent") or "").strip()
    query = str(params.get("q") or "").strip()
    date_from = parse_date(str(params.get("date_from") or ""))
    date_to = parse_date(str(params.get("date_to") or ""))

    if status in CallRecord.Status.values:
        qs = qs.filter(status=status)
    if disposition == "unclassified":
        qs = qs.filter(disposition="")
    elif disposition:
        qs = qs.filter(disposition=disposition)
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
    if params.get("crm") == "linked":
        qs = qs.filter(lead__isnull=False)
    elif params.get("crm") == "unlinked":
        qs = qs.filter(lead__isnull=True)
    if params.get("lead"):
        try:
            qs = qs.filter(lead_id=UUID(str(params["lead"])))
        except (ValueError, TypeError):
            qs = qs.none()
    if date_from:
        qs = qs.filter(ended_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(ended_at__date__lte=date_to)
    return qs


def _activity_url(request, **changes):
    params = request.GET.copy()
    params["section"] = "analytics"
    params.pop("page", None)
    params.pop("open", None)
    for key, value in changes.items():
        if value:
            params[key] = value
        else:
            params.pop(key, None)
    return reverse("call-intelligence-dashboard") + "?" + params.urlencode() + "#calls"


@crm_login_required
def call_intelligence_dashboard(request):
    user = request.crm_user
    reconcile_call_tracking(user)
    settings_obj, _ = CallIntelligenceSettings.objects.get_or_create(
        organization=user.organization
    )
    calls = _filtered_calls(request, user)
    today = calls  # All analytics share the activity filters, including dates.
    stats = call_metrics(today)

    page = Paginator(calls.select_related(
        "lead", "lead__pipeline", "lead__stage", "user", "intelligence"
    ).order_by("-ended_at", "-created_at"), 50).get_page(request.GET.get("page"))
    recent_calls = list(page.object_list)
    accessible_pipeline_ids = set(get_user_pipelines(user).values_list("id", flat=True))
    dispositions = list(get_call_dispositions(user.organization))
    all_dispositions = list(CallDisposition.objects.filter(organization=user.organization))
    disposition_by_code = {row.code: row for row in all_dispositions}
    for call in recent_calls:
        outcome = disposition_by_code.get(call.disposition)
        call.outcome_label = outcome.name if outcome else (call.disposition.replace("_", " ").title() or "Not classified")
        call.outcome_category = outcome.category if outcome else "unclassified"
        call.outcome_is_active = bool(outcome and outcome.is_active)
        call.crm_url = ""
        if call.lead_id and call.lead.pipeline_id in accessible_pipeline_ids:
            call.crm_url = reverse("crm-dashboard") + "?" + urlencode({
                "pipeline": call.lead.pipeline_id, "stage": call.lead.stage_id, "lead": call.lead_id,
            })
    outcome_groups = call_outcome_groups(_filtered_calls(request, user, ignore=("disposition",)), all_dispositions)
    selected_outcome = str(request.GET.get("disposition") or "").strip()
    for group in outcome_groups:
        group["url"] = _activity_url(request, disposition=group["code"])
        group["selected"] = group["code"] == selected_outcome
    active_outcome = next((group["name"] for group in outcome_groups if group["selected"]), "All calls")
    crm_counts = _filtered_calls(request, user, ignore=("crm",)).aggregate(
        total=Count("id"), linked=Count("id", filter=Q(lead__isnull=False)),
        unlinked=Count("id", filter=Q(lead__isnull=True)),
    )
    crm_groups = [
        {"code": "", "name": "All contacts", "total": crm_counts["total"]},
        {"code": "linked", "name": "CRM leads", "total": crm_counts["linked"]},
        {"code": "unlinked", "name": "Needs a lead", "total": crm_counts["unlinked"]},
    ]
    for group in crm_groups:
        group["url"] = _activity_url(request, crm=group["code"])
        group["selected"] = group["code"] == str(request.GET.get("crm") or "")
    lead_destinations = [
        {"id": str(p.id), "name": p.name, "owned": p.owner_id == user.id,
         "stages": [{"id": str(s.id), "name": s.name} for s in p.stages.all() if s.is_active]}
        for p in get_user_pipelines(user).prefetch_related("stages")
    ]
    contact_history_name = ""
    if request.GET.get("lead"):
        try:
            contact_history_name = _scoped_calls(user).filter(
                lead_id=UUID(str(request.GET["lead"])),
            ).values_list("lead__name", flat=True).first() or ""
        except (ValueError, TypeError):
            pass
    page_query = request.GET.copy()
    page_query["section"] = "analytics"
    page_query.pop("page", None)
    missed_calls = list(
        calls.filter(status=CallRecord.Status.MISSED)
        .select_related("lead", "user")
        .order_by("-ended_at", "-created_at")[:12]
    )
    device_qs = CallDevice.objects.filter(organization=user.organization, is_active=True)
    if user.role != User.Role.ADMIN:
        device_qs = device_qs.filter(user=user)
    devices = list(
        device_qs
        .select_related("user")
        .order_by("-last_seen_at", "user__name")
    )
    agent_stats = team_metrics(today)
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
        "active_section": "analytics" if request.GET.get("section") == "analytics" or any(
            request.GET.get(k) for k in ("q", "status", "disposition", "direction", "source", "intent", "pipeline", "agent", "date_from", "date_to", "crm", "lead")
        ) else "overview",
        "call_page": page,
        "page_query": page_query.urlencode(),
        "call_settings": settings_obj,
        "recent_calls": recent_calls,
        "contact_history_name": contact_history_name,
        "contact_history_clear_url": _activity_url(request, lead=""),
        "outcome_groups": outcome_groups,
        "active_outcome": active_outcome,
        "crm_groups": crm_groups,
        "lead_destinations": lead_destinations,
        "can_create_call_lead": any(p["stages"] for p in lead_destinations),
        "missed_calls": missed_calls,
        "devices": devices,
        "agent_stats": agent_stats,
        "dispositions": dispositions,
        "pipelines": pipelines,
        "stages": Stage.objects.filter(
            pipeline__organization=user.organization,
            pipeline__is_active=True,
            is_active=True,
        ).select_related("pipeline").order_by("pipeline__name", "display_order"),
        "owners": owners,
        "stats": stats,
        "filters": {
            key: str(request.GET.get(key) or "")
            for key in (
                "q", "status", "disposition", "direction", "source", "intent",
                "pipeline", "agent", "date_from", "date_to", "crm", "lead",
            )
        },
        "release": release,
        "release_url": release_url,
        "is_admin": user.role == User.Role.ADMIN,
        "call_statuses": CallRecord.Status.choices,
        "call_directions": CallRecord.Direction.choices,
        "call_sources": [("android_sim", "Android APK · SIM"), ("manual", "CRM · Manual call"), ("cloud", "Cloud · API events")],
        "source_counts": list(calls.values("source").annotate(total=Count("id")).order_by("source")),
        "call_intents": CallIntelligenceResult.Intent.choices,
    })


@crm_login_required
@require_POST
def create_call_lead(request, call_id):
    user = request.crm_user
    if not _scoped_calls(user).filter(pk=call_id).exists():
        raise Http404("Call not found.")
    try:
        call, created = create_crm_lead_from_call(
            user=user, call_id=call_id, name=request.POST.get("name"),
            pipeline_id=request.POST.get("pipeline"), stage_id=request.POST.get("stage"),
            email=request.POST.get("email", ""),
        )
    except ValidationError as exc:
        return JsonResponse({"ok": False, "error": " ".join(exc.messages)}, status=400)
    # Show this contact's complete call history after creation, focused on the
    # original call even when its history spans several pages.
    history = _scoped_calls(user).filter(lead_id=call.lead_id)
    if call.ended_at:
        ahead = history.filter(Q(ended_at__isnull=True) | Q(ended_at__gt=call.ended_at)
                               | Q(ended_at=call.ended_at, created_at__gt=call.created_at))
    else:
        ahead = history.filter(ended_at__isnull=True, created_at__gt=call.created_at)
    redirect_url = reverse("call-intelligence-dashboard") + "?" + urlencode({
        "section": "analytics", "lead": call.lead_id,
        "page": ahead.count() // 50 + 1, "open": call.id,
    }) + f"#call-{call.id}"
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"ok": True, "created": created, "lead_id": str(call.lead_id), "redirect_url": redirect_url})
    return redirect(redirect_url)


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
    if disposition and disposition != call.disposition and not get_call_dispositions(user.organization).filter(
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
        call = _scoped_calls(user).select_for_update().get(pk=call_id)
        outcome_changed = call.disposition != disposition
        if due_at and not call.lead_id:
            return JsonResponse(
                {"ok": False, "error": "A CRM lead is required for a reminder."}, status=400,
            )
        call.notes = notes
        call.disposition = disposition
        call.save(update_fields=["notes", "disposition", "updated_at"])
        if call.crm_call_id:
            call.crm_call.notes = notes
            call.crm_call.save(update_fields=["notes"])

        if due_at:
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
        request_call_analysis(call.id, retry_failed=True)

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"ok": True, "status_url": reverse(
            "call-intelligence-call-status", args=[call.id],
        ), "outcome_changed": outcome_changed, "activity_url": _activity_url(
            request, disposition=disposition, open=str(call.id),
        )})
    return HttpResponseRedirect(reverse("call-intelligence-dashboard") + "?section=analytics#calls")


@crm_login_required
@require_GET
def call_analysis_status(request, call_id):
    call = _scoped_calls(request.crm_user).select_related("intelligence").filter(pk=call_id).first()
    if call is None:
        raise Http404("Call not found.")
    intelligence = getattr(call, "intelligence", None)
    response = JsonResponse({
        "ok": True,
        "analysis_status": "completed" if intelligence else call.analysis_status,
        "analysis_error": call.analysis_error,
        "badge_html": render_to_string("telephony/partials/intelligence_badge.html", {"call": call}),
        "intelligence_html": render_to_string("telephony/partials/intelligence_detail.html", {"call": call}),
    })
    response["Cache-Control"] = "no-store"
    return response


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


@crm_login_required
@require_POST
def remove_call_device(request, device_id):
    user = request.crm_user
    qs = CallDevice.objects.filter(organization=user.organization, pk=device_id)
    if user.role != User.Role.ADMIN:
        qs = qs.filter(user=user)
    if not qs.update(is_active=False, updated_at=timezone.now()):
        raise Http404("Device not found.")
    return redirect(reverse("call-intelligence-dashboard") + "#devices")
