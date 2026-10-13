"""Read-only API Operations Center, protected by the Superadmin session."""

from urllib.parse import urlencode, urlsplit

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from apps.core.api_activity.coverage import INTEGRATIONS
from apps.core.api_activity.queries import (
    InvalidActivityFilter, RANGES, STATUS_LABELS, activity_detail, explorer_report, parse_filters,
)
from apps.core.api_activity.services import capture_health
from apps.core.models import APIActivity
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
    return render(request, "superadmin/api/detail.html", _detail(request, activity_id))


@never_cache
@superuser_required
@require_GET
def api_activity_data_view(request, activity_id):
    return JsonResponse(_detail(request, activity_id))
