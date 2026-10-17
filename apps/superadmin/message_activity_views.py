"""Superadmin-only HTML and Excel entry points for organisation activity."""

from contextlib import closing
from dataclasses import replace
from tempfile import TemporaryFile
from urllib.parse import urlencode

from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from apps.organizations.models import Organization
from .message_activity import (
    activity_count, activity_page, filter_options, get_activity, iter_activity, parse_filters,
)
from .message_activity_export import MAX_EXPORT_ROWS, ExportTooLarge, write_activity_workbook
from .views_flat import superuser_required


def _private(response):
    response["Cache-Control"] = "private, no-store"
    response["X-Robots-Tag"] = "noindex, nofollow"
    response["Referrer-Policy"] = "same-origin"
    return response


def _url(organization, name="superadmin-organization-message-activity", *, params=None, kind=None, record_id=None):
    args = [organization.pk]
    if kind is not None:
        args.extend((kind, record_id))
    value = reverse(name, args=args)
    query = urlencode({key: item for key, item in (params or {}).items() if item not in (None, "")})
    return value + ("?" + query if query else "")


def _links(organization, filters, page=1):
    params = {**filters.values, "page": page}
    return {
        "index": _url(organization),
        "org": reverse("superadmin-organization-detail", args=[organization.pk]),
        "export": _url(organization, "superadmin-organization-message-activity-export", params=filters.values),
        "refresh": _url(organization, params=params),
        "previous": _url(organization, params={**params, "page": max(1, page - 1)}),
        "next": _url(organization, params={**params, "page": page + 1}),
    }


def _row_links(organization, row, filters, page=1):
    params = {**filters.values, "page": page}
    row["detail_url"] = _url(organization, "superadmin-organization-message-activity-detail",
                             params=params, kind=row["kind"], record_id=row["id"])
    for related in row.get("related", ()):
        related["url"] = _url(organization, "superadmin-organization-message-activity-detail",
                              params=params, kind=related["kind"], record_id=related["id"])
    if row["kind"] in {"whatsapp", "instagram"}:
        row["api_url"] = reverse("superadmin-api") + "?" + urlencode({
            "organization": str(organization.pk), "q": row["id"], "range": "90d",
        })


def _error(request, organization, message, *, status=400, filters=None):
    if filters is None:
        filters = parse_filters(organization, {})
        # Retain the submitted controls, but never display a broader fallback
        # result set when any filter was invalid or from a different tenant.
        values = {key: str(request.GET.get(key, value))[:200] for key, value in filters.values.items()}
        filters = replace(filters, values=values)
    with timezone.override(filters.tz):
        return _private(render(request, "superadmin/message_activity.html", {
            "organization": organization, "filters": filters, "options": filter_options(organization),
            "activity": {"rows": [], "total": 0, "page": 1, "num_pages": 1, "summary": [],
                         "has_previous": False, "has_next": False},
            "urls": _links(organization, filters), "error": message,
        }, status=status))


def _page_number(request):
    value = request.GET.get("page", "1")
    if not str(value).isdigit() or len(str(value)) > 9 or int(value) < 1:
        raise ValueError("Choose a valid activity page.")
    return int(value)


@never_cache
@superuser_required
@require_GET
def organization_message_activity(request, organization_id):
    organization = get_object_or_404(Organization, pk=organization_id)
    try:
        filters = parse_filters(organization, request.GET)
        result = activity_page(organization, filters, page=_page_number(request))
    except ValueError as exc:
        return _error(request, organization, str(exc))
    for row in result["rows"]:
        _row_links(organization, row, filters, result["page"])
    with timezone.override(filters.tz):
        response = render(request, "superadmin/message_activity.html", {
            "organization": organization, "filters": filters, "options": filter_options(organization),
            "activity": result, "urls": _links(organization, filters, result["page"]),
        })
    return _private(response)


@never_cache
@superuser_required
@require_GET
def organization_message_activity_detail(request, organization_id, kind, record_id):
    organization = get_object_or_404(Organization, pk=organization_id)
    try:
        filters = parse_filters(organization, request.GET)
        page = _page_number(request)
    except ValueError as exc:
        return _error(request, organization, str(exc))
    try:
        row = get_activity(organization, kind, record_id)
    except (ValueError, TypeError) as exc:
        raise Http404("Activity not found for this organisation.") from exc
    if not row:
        raise Http404("Activity not found for this organisation.")
    _row_links(organization, row, filters, page)
    urls = _links(organization, filters, page)
    urls["index"] = urls["refresh"]
    with timezone.override(filters.tz):
        response = render(request, "superadmin/message_activity_detail.html", {
            "organization": organization, "filters": filters, "activity_row": row, "urls": urls,
        })
    return _private(response)


@never_cache
@superuser_required
@require_GET
def organization_message_activity_export(request, organization_id):
    organization = get_object_or_404(Organization, pk=organization_id)
    try:
        filters = parse_filters(organization, request.GET)
    except ValueError as exc:
        return _error(request, organization, str(exc))
    if activity_count(organization, filters) > MAX_EXPORT_ROWS:
        return _error(request, organization,
                      f"More than {MAX_EXPORT_ROWS:,} activities match. Narrow the dates or filters and export again.",
                      status=422, filters=filters)
    output = TemporaryFile(mode="w+b")
    try:
        with closing(iter_activity(organization, filters)) as rows:
            write_activity_workbook(output, organization=organization, filters=filters,
                                    rows=rows, max_rows=MAX_EXPORT_ROWS)
        output.seek(0)
    except ExportTooLarge as exc:
        output.close()
        return _error(request, organization, str(exc), status=422, filters=filters)
    except Exception:
        output.close()
        raise
    filename = f"shvya-message-activity-{organization.pk}-{filters.from_date}-{filters.to_date}.xlsx"
    response = FileResponse(output, as_attachment=True, filename=filename,
                            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["X-Content-Type-Options"] = "nosniff"
    return _private(response)
