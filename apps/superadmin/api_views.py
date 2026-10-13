"""API evidence and investigation controls, protected by the Superadmin session."""

from urllib.parse import urlencode, urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST
from django.utils import timezone

from apps.core.api_activity.health import operations_health
from apps.core.api_activity.coverage import INTEGRATIONS
from apps.core.api_activity.queries import (
    InvalidActivityFilter, RANGES, STATUS_LABELS, activity_detail, explorer_report, parse_filters, serialize_activity,
)
from apps.core.api_activity.services import capture_health
from apps.core.api_activity.operations import (
    InvestigationError, StaleInvestigation, journey_report, needs_attention,
    saved_outcome, serialize_incident, update_incident,
)
from apps.core.api_activity.sanitization import sanitize_metadata, sanitize_text
from apps.core.models import APIActivity, APIIncident
from apps.organizations.models import Organization

from .views_flat import superuser_required


def _pagination_links(request, pagination, *, parameter="page"):
    links = {}
    for name, offset in (("previous", -1), ("next", 1)):
        if pagination[f"has_{name}"]:
            params = request.GET.copy()
            params[parameter] = pagination["page"] + offset
            links[name] = "?" + params.urlencode()
    return links


def _report(request):
    report = explorer_report(parse_filters(request.GET))
    report["capture_health"] = capture_health()
    report["capture_health"]["enabled"] = bool(getattr(settings, "API_ACTIVITY_ENABLED", True))
    return report


def _explorer_url(filters, *, status="", organization=None):
    params = {
        key: value for key, value in filters.items()
        if key in {"range", "organization", "integration", "kind", "q", "per_page"}
        and value and not (key == "per_page" and value == 25)
    }
    if organization is not None:
        params["organization"] = organization
        params.pop("q", None)
    if status:
        params["status"] = status
    return reverse("superadmin-api") + "?" + urlencode(params)


@never_cache
@superuser_required
@require_GET
def api_operations_view(request):
    try:
        report = _report(request)
    except InvalidActivityFilter as exc:
        return render(request, "superadmin/api/filter_error.html", {"filter_error": str(exc)}, status=400)
    report["organization_health"] = operations_health(hours={"24h": 24, "7d": 168, "30d": 720, "90d": 720}.get(report["filters"]["range"], 24), organization_id=report["filters"]["organization"] or None)["organizations"][:12]
    report.update({
        "organizations": Organization.objects.only("id", "name").order_by("name")[:500],
        "integration_options": [(key, label) for key, label, _, _ in INTEGRATIONS],
        "range_options": [(key, value[1]) for key, value in RANGES.items()],
        "status_options": [("attention", "Needs attention"), ("waiting", "Waiting for next step"), ("overdue", "Overdue"), *STATUS_LABELS.items()],
        "pagination_links": _pagination_links(request, report["pagination"]),
        "json_url": reverse("superadmin-api-data") + ("?" + request.GET.urlencode() if request.GET else ""),
    })
    report["summary_links"] = {
        key: _explorer_url(report["filters"], status=status)
        for key, status in (("all", ""), ("completed", "completed"),
                            ("attention", "attention"), ("waiting", "waiting"),
                            ("overdue", "overdue"), ("unknown", "unknown"))
    }
    back = urlencode({"back": request.get_full_path()})
    for row in (*report["results"], *report["attention_items"]):
        row["url"] += "?" + back
        if row["organization_id"]:
            row["organization_url"] = _explorer_url(
                report["filters"], status="attention", organization=row["organization_id"]
            )
    for group in report["incidents"]:
        params = {key: value for key, value in report["filters"].items() if key in {"range", "organization", "kind"} and value}
        params.update({"integration": group["integration"], "status": group["status"]})
        if group["error_code"]:
            params["q"] = group["error_code"]
        group["url"] = reverse("superadmin-api") + "?" + urlencode(params)
    rows = [*report["results"], *report["attention_items"]]
    investigations = {str(item.activity_id): serialize_incident(item) for item in APIIncident.objects.filter(
        activity_id__in=[row["id"] for row in rows]
    ).select_related("owner")}
    for row in rows:
        row["investigation"] = investigations.get(row["id"])
    return render(request, "superadmin/api/index.html", report)


@never_cache
@superuser_required
@require_GET
def api_operations_data_view(request):
    try:
        report = _report(request)
    except InvalidActivityFilter as exc:
        return JsonResponse({"error": "invalid_filter", "message": str(exc)}, status=400)
    return JsonResponse(report)


def _detail(request, activity_id):
    activity = get_object_or_404(APIActivity.objects.select_related("organization"), pk=activity_id)
    try:
        event_page = int(request.GET.get("event_page", 1))
    except (TypeError, ValueError):
        event_page = 1
    detail = activity_detail(activity, event_page=event_page)
    detail["journey"] = journey_report(activity)
    detail["saved_outcome"] = saved_outcome(activity)
    incident = APIIncident.objects.filter(activity=activity).select_related("owner").first()
    detail["investigation"] = serialize_incident(incident) if incident else None
    detail["can_open_investigation"] = needs_attention(activity)
    detail["investigation_events"] = [{
        "action": event.action, "note": sanitize_text(event.note, max_length=500),
        "actor": event.actor.email if event.actor else "Former operator",
        "created_at": event.created_at, "metadata": sanitize_metadata(event.metadata),
    } for event in incident.events.select_related("actor")[:50]] if incident else []
    detail["pagination_links"] = _pagination_links(request, detail["event_pagination"], parameter="event_page")
    back = request.GET.get("back", "")
    try:
        parsed = urlsplit(back)
    except ValueError:
        parsed = urlsplit("")
    detail["back_url"] = (
        back if len(back) <= 1200 and not parsed.scheme and not parsed.netloc
        and not parsed.fragment and parsed.path == reverse("superadmin-api")
        else reverse("superadmin-api")
    )
    return detail


@never_cache
@superuser_required
@require_GET
def api_activity_view(request, activity_id):
    detail = _detail(request, activity_id)
    detail["incident_owners"] = get_user_model().objects.filter(is_active=True, is_superuser=True).only("id", "email").order_by("email")[:100]
    return render(request, "superadmin/api/detail.html", detail)


@never_cache
@superuser_required
@require_GET
def api_activity_data_view(request, activity_id):
    return JsonResponse(_detail(request, activity_id))


@never_cache
@superuser_required
@require_POST
def api_incident_update_view(request, activity_id):
    get_object_or_404(APIActivity, pk=activity_id)
    try:
        update_incident(activity_id, request.user, request.POST)
    except InvestigationError as exc:
        return render(request, "superadmin/api/investigation_error.html", {
            "error": str(exc), "activity_id": activity_id,
        }, status=409 if isinstance(exc, StaleInvestigation) else 400)
    return redirect("superadmin-api-activity", activity_id=activity_id)


@never_cache
@superuser_required
@require_GET
def api_incidents_view(request):
    try:
        filters = parse_filters(request.GET)
        review = request.GET.get("review", "active")
        owner = request.GET.get("owner", "all")
        severity = request.GET.get("severity", "")
        if review not in {"active", "open", "acknowledged", "resolved", "all", "overdue"} or owner not in {"all", "me", "unassigned"} or severity not in {"", "low", "medium", "high"}:
            raise InvalidActivityFilter("Choose supported investigation filters.")
    except InvalidActivityFilter as exc:
        return render(request, "superadmin/api/filter_error.html", {"filter_error": str(exc)}, status=400)
    rows = APIIncident.objects.select_related("activity__organization", "owner")
    if filters["organization"]:
        rows = rows.filter(activity__organization_id=filters["organization"])
    if filters["integration"]:
        rows = rows.filter(activity__integration=filters["integration"])
    now = timezone.now()
    overdue = Q(due_at__lt=now) & ~Q(status="resolved") & (Q(snoozed_until__isnull=True) | Q(snoozed_until__lte=now))
    if owner == "me":
        rows = rows.filter(owner=request.user)
    elif owner == "unassigned":
        rows = rows.filter(owner__isnull=True)
    if severity:
        rows = rows.filter(severity=severity)
    scope = rows
    if review == "active":
        rows = rows.exclude(status="resolved")
    elif review == "overdue":
        rows = rows.filter(overdue)
    elif review != "all":
        rows = rows.filter(status=review)
    paginator = Paginator(rows.order_by("due_at", "-created_at", "pk"), 50)
    page = paginator.get_page(filters["page"])
    return render(request, "superadmin/api/incidents.html", {
        "results": [{"incident": serialize_incident(row), "activity": serialize_activity(row.activity)} for row in page],
        "review": review, "owner": owner, "severity": severity, "filters": filters,
        "active_count": scope.exclude(status="resolved").count(), "overdue_count": scope.filter(overdue).count(),
        "page": page, "pagination_links": _pagination_links(request, {
            "page": page.number, "has_previous": page.has_previous(), "has_next": page.has_next(),
        }),
    })


@never_cache
@superuser_required
@require_GET
def api_health_data_view(request):
    """Tenant-filterable operational health; no raw payloads or provider calls."""
    try:
        hours = int(request.GET.get("hours", "24"))
        organization = parse_filters({"organization": request.GET.get("organization", "")})["organization"]
        result = operations_health(hours=hours, organization_id=organization or None)
    except (ValueError, TypeError, InvalidActivityFilter):
        return JsonResponse({"error": "invalid_filter"}, status=400)
    return JsonResponse(result)
