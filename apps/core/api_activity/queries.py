"""Bounded, read-only queries for the explicit global Superadmin boundary."""

from datetime import timedelta
import json
import re
import uuid
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.db.models import Avg, Count, Max, Min, Q
from django.urls import reverse
from django.utils import timezone

from apps.core.api_activity.coverage import INTEGRATION_LABELS, coverage_snapshot
from apps.core.api_activity.sanitization import sanitize_endpoint, sanitize_identifier, sanitize_metadata, sanitize_text
from apps.core.models import APIActivity


STATUS_LABELS = {
    "queued": "Queued", "processing": "Processing", "accepted": "Accepted",
    "completed": "Completed", "failed": "Failed", "unknown": "Unknown outcome",
    "blocked": "Blocked", "skipped": "Skipped", "partial": "Partially completed",
}
OPEN_STATUSES = ("queued", "processing", "accepted")
ISSUE_STATUSES = ("failed", "unknown", "blocked", "partial")
# Values remain days for existing callers. One hour also supports exact health-API drill-downs.
RANGES = {
    "1h": (1 / 24, "Last hour"), "24h": (1, "Last 24 hours"),
    "7d": (7, "Last 7 days"), "30d": (30, "Last 30 days"), "90d": (90, "Last 90 days"),
}
RANGE_BY_HOURS = {round(days * 24): key for key, (days, _) in RANGES.items()}
UNATTRIBUTED = "unattributed"


class InvalidActivityFilter(ValueError):
    pass


def parse_filters(params):
    selected_range = params.get("range", "24h")
    if selected_range not in RANGES:
        raise InvalidActivityFilter("Choose a time range of 1 or 24 hours, 7, 30 or 90 days.")
    organization = params.get("organization", "").strip()
    if organization and organization != UNATTRIBUTED:
        try:
            organization = str(uuid.UUID(organization))
        except (ValueError, TypeError, AttributeError):
            raise InvalidActivityFilter("Enter a valid organization ID or choose unattributed activity.") from None
    status = params.get("status", "").strip()
    if status and status not in {*STATUS_LABELS, "overdue", "attention", "waiting"}:
        raise InvalidActivityFilter("Choose a supported activity outcome.")
    integration = params.get("integration", "").strip()
    if integration and not re.fullmatch(r"[a-zA-Z0-9_.-]{1,48}", integration):
        raise InvalidActivityFilter("Choose a supported integration.")
    kind = params.get("kind", "").strip()
    if kind and not re.fullmatch(r"[a-zA-Z0-9_.-]{1,48}", kind):
        raise InvalidActivityFilter("Choose a supported activity type.")
    try:
        page = max(1, min(int(params.get("page", 1)), 1000))
        per_page = max(10, min(int(params.get("per_page", 25)), 100))
    except (TypeError, ValueError):
        raise InvalidActivityFilter("Page and page size must be numbers.") from None
    return {
        "range": selected_range, "range_label": RANGES[selected_range][1],
        "organization": organization, "integration": integration, "status": status,
        "kind": kind, "q": sanitize_identifier(params.get("q", "").strip(), max_length=160),
        "page": page, "per_page": per_page,
    }


def organization_scope(queryset, organization, *, field="organization_id"):
    """Apply an already validated tenant selection without broadening a null scope."""
    if organization == UNATTRIBUTED:
        return queryset.filter(**{field + "__isnull": True})
    if organization:
        return queryset.filter(**{field: organization})
    return queryset


def explorer_url(filters, *, status="", organization=None):
    """Change outcome/tenant only; keep the exact search scope and reset pagination."""
    params = {
        key: value for key, value in filters.items()
        if key in {"range", "organization", "integration", "kind", "q", "per_page"}
        and value and not (key == "per_page" and value == 25)
    }
    if organization is not None:
        params["organization"] = organization
    if status:
        params["status"] = status
    return reverse("superadmin-api") + "?" + urlencode(params)


def overdue_filter(now):
    return Q(status__in=OPEN_STATUSES, expected_by__lt=now)


def attention_filter(now):
    """The same attention definition for explorer, health and review discovery."""
    return Q(status__in=ISSUE_STATUSES) | overdue_filter(now)


def waiting_filter():
    return Q(status__in=OPEN_STATUSES, expected_by__isnull=False)


def diagnosis(activity):
    """Explain recorded classifications without inventing a provider root cause."""
    code = str(activity.error_code or "")
    category = str(activity.error_category or "").lower()
    if activity.status == "unknown":
        return {"evidence": "Outcome unconfirmed", "explanation": "The available record does not establish whether the external action completed.",
                "action": "Match the provider reference against recorded callbacks or the provider's supported history before considering another send."}
    if activity.http_status in (401, 403) or category in {"authentication", "authorization", "permission", "auth"}:
        return {"evidence": "Recorded authentication or permission response", "explanation": "The request was rejected at an authentication or access boundary.",
                "action": "Check the identified connection's credential validity, granted permissions and organization routing. The response alone does not identify which setting changed."}
    if activity.http_status == 429 or category in {"rate_limit", "rate_limited"}:
        return {"evidence": "Recorded throttling response", "explanation": "The request reached a rate or usage limit.",
                "action": "Inspect the provider's error code, quota and retry guidance. Confirm existing retry scheduling before adding another attempt."}
    if code == "131042":
        return {"evidence": "Recorded Meta error 131042", "explanation": "Meta reported a payment or business-eligibility error for this request.",
                "action": "Review billing and account eligibility in the linked WhatsApp business account; use the provider error details to verify the exact restriction."}
    if activity.http_status and activity.http_status >= 500:
        return {"evidence": "Recorded server error response", "explanation": "A server returned an error at this recorded request boundary.",
                "action": "Inspect the endpoint, correlated worker/request events and provider status. A server error alone does not establish whether a prior side effect occurred."}
    if activity.status in {"failed", "blocked", "partial"}:
        return {"evidence": "Recorded operation outcome", "explanation": "This operation recorded a failure, blocking condition or partial completion. Its summary and timeline contain the available cause evidence.",
                "action": "Inspect the error code, connection, entity reference and related activities. Correct only the condition supported by those records."}
    return {"evidence": "Recorded step only", "explanation": "This record describes one operation at its capture boundary.",
            "action": "Use the timeline and related activities to establish downstream processing or provider delivery."}


def filtered_activities(filters, *, now=None):
    now = now or timezone.now()
    queryset = APIActivity.objects.filter(created_at__gte=now - timedelta(days=RANGES[filters["range"]][0]))
    queryset = organization_scope(queryset, filters["organization"])
    if filters["integration"] == "celery":
        queryset = queryset.filter(kind="task")
    elif filters["integration"]:
        queryset = queryset.filter(integration=filters["integration"])
    if filters["kind"]:
        queryset = queryset.filter(kind=filters["kind"])
    if filters["status"] == "overdue":
        queryset = queryset.filter(overdue_filter(now))
    elif filters["status"] == "waiting":
        queryset = queryset.filter(waiting_filter())
    elif filters["status"] == "attention":
        queryset = queryset.filter(attention_filter(now))
    elif filters["status"]:
        queryset = queryset.filter(status=filters["status"])
    if filters["q"]:
        query = filters["q"]
        search = (
            Q(organization__name__icontains=query) | Q(provider_reference__icontains=query)
            | Q(error_code__icontains=query) | Q(operation__icontains=query)
            | Q(request_id__icontains=query) | Q(task_id__icontains=query)
            | Q(entity_id__icontains=query) | Q(connection_id__icontains=query)
        )
        try:
            identifier = uuid.UUID(query)
        except (ValueError, TypeError):
            pass
        else:
            search |= Q(pk=identifier) | Q(correlation_id=identifier) | Q(organization_id=identifier)
        queryset = queryset.filter(search)
    return queryset


def serialize_activity(activity, *, now=None, detail=False):
    now = now or timezone.now()
    status = activity.status
    result = {
        "id": str(activity.pk), "correlation_id": str(activity.correlation_id),
        "parent_id": str(activity.parent_id) if activity.parent_id else None,
        "organization_id": str(activity.organization_id) if activity.organization_id else None,
        "organization_name": activity.organization.name if activity.organization else "Unattributed / platform",
        "organization_reference": sanitize_identifier(activity.organization_reference, max_length=120),
        "integration": sanitize_text(activity.integration, max_length=48),
        "integration_label": INTEGRATION_LABELS.get(activity.integration, activity.integration.replace("_", " ").title()),
        "provider": sanitize_text(activity.provider, max_length=48),
        "kind": sanitize_text(activity.kind, max_length=48),
        "direction": sanitize_text(activity.direction, max_length=16),
        "operation": sanitize_text(activity.operation, max_length=200),
        "status": status, "status_label": STATUS_LABELS.get(status, "Unknown outcome"),
        "is_overdue": bool(status in OPEN_STATUSES and activity.expected_by and activity.expected_by < now),
        "http_status": activity.http_status, "duration_ms": activity.duration_ms,
        "method": sanitize_text(activity.method, max_length=16),
        "endpoint": sanitize_endpoint(activity.endpoint),
        "provider_reference": sanitize_identifier(activity.provider_reference, max_length=200),
        "error_code": sanitize_text(activity.error_code, max_length=100),
        "error_category": sanitize_text(activity.error_category, max_length=100),
        "error_message": sanitize_text(activity.error_message, max_length=1000),
        "summary": sanitize_text(activity.summary, max_length=1000),
        "next_step": sanitize_text(activity.next_step, max_length=500),
        "created_at": activity.created_at, "updated_at": activity.updated_at,
        "started_at": activity.started_at, "finished_at": activity.finished_at,
        "expected_by": activity.expected_by,
        "url": reverse("superadmin-api-activity", kwargs={"activity_id": activity.pk}),
    }
    if detail:
        result.update({
            key: sanitize_identifier(getattr(activity, key), max_length=200)
            for key in ("connection_id", "entity_type", "entity_id", "request_id", "task_id")
        })
        result["metadata"] = sanitize_metadata(activity.metadata)
    return result


def explorer_report(filters, *, now=None):
    now = now or timezone.now()
    queryset = filtered_activities(filters, now=now)
    # Keep overview counts aligned with the links that change the outcome filter.
    # Time, organization, integration, type and search remain in scope.
    overview = filtered_activities({**filters, "status": ""}, now=now)
    summary = overview.aggregate(
        total=Count("pk"), completed=Count("pk", filter=Q(status="completed")),
        attention=Count("pk", filter=attention_filter(now)),
        waiting=Count("pk", filter=waiting_filter()),
        overdue=Count("pk", filter=overdue_filter(now)),
        unknown=Count("pk", filter=Q(status="unknown")),
        average_duration_ms=Avg("duration_ms"), last_activity_at=Max("created_at"),
    )
    if summary["average_duration_ms"] is not None:
        summary["average_duration_ms"] = round(summary["average_duration_ms"], 1)
    paginator = Paginator(queryset.select_related("organization").defer("metadata").order_by("-created_at", "-id"), filters["per_page"])
    page = paginator.get_page(filters["page"])
    groups = list(queryset.filter(status__in=ISSUE_STATUSES).values(
        "integration", "error_category", "error_code", "status"
    ).annotate(count=Count("pk"), organizations=Count("organization_id", distinct=True),
               first_seen=Min("created_at"), last_seen=Max("created_at")).order_by("-count", "-last_seen")[:8])
    for group in groups:
        for key in ("integration", "error_category", "error_code"):
            group[key] = sanitize_text(group[key], max_length=100)
        group["integration_label"] = INTEGRATION_LABELS.get(group["integration"], group["integration"])
        group["label"] = sanitize_text(group["error_code"] or group["error_category"] or STATUS_LABELS.get(group["status"], "Unknown"), max_length=100)
    attention_rows = overview.filter(attention_filter(now)).select_related(
        "organization"
    ).defer("metadata").order_by("-created_at", "-id")[:8]
    attention_items = []
    for activity in attention_rows:
        item = serialize_activity(activity, now=now)
        item["attention_label"] = "Overdue" if item["is_overdue"] else item["status_label"]
        item["attention_status"] = "overdue" if item["is_overdue"] else item["status"]
        if item["is_overdue"]:
            item["investigation_hint"] = item["next_step"] or "Check the last recorded step and its related activity."
        else:
            item["investigation_hint"] = item["summary"] or diagnosis(activity)["action"]
        attention_items.append(item)
    return {
        "generated_at": now, "filters": filters, "summary": summary,
        "results": [serialize_activity(activity, now=now) for activity in page.object_list],
        "pagination": {"page": page.number, "pages": min(paginator.num_pages, 1000), "total": paginator.count,
                       "per_page": filters["per_page"], "has_next": page.has_next() and page.number < 1000,
                       "has_previous": page.has_previous(), "truncated": paginator.num_pages > 1000},
        "incidents": groups, "coverage": coverage_snapshot(queryset),
        "attention_items": attention_items,
    }


def activity_detail(activity, *, event_page=1):
    result = serialize_activity(activity, detail=True)
    result["diagnosis"] = diagnosis(activity)
    event_paginator = Paginator(activity.events.order_by("timestamp", "pk"), 50)
    page = event_paginator.get_page(max(1, min(event_page, 1000)))
    events = []
    for event in page.object_list:
        metadata = sanitize_metadata(event.metadata)
        events.append({
            "timestamp": event.timestamp, "name": sanitize_text(event.name, max_length=160),
            "status": event.status, "status_label": STATUS_LABELS.get(event.status, event.status),
            "summary": sanitize_text(event.summary, max_length=1000), "metadata": metadata,
            "metadata_display": json.dumps(metadata, indent=2, ensure_ascii=False),
        })
    last_event = activity.events.order_by("-timestamp", "-pk").first()
    relation = Q(correlation_id=activity.correlation_id)
    if activity.organization_id and activity.entity_type and activity.entity_id:
        relation |= Q(entity_type=activity.entity_type, entity_id=activity.entity_id)
    if activity.organization_id and activity.provider_reference:
        relation |= Q(integration=activity.integration, provider_reference=activity.provider_reference)
    related = APIActivity.objects.filter(relation, organization_id=activity.organization_id).exclude(pk=activity.pk).select_related("organization").defer("metadata").order_by("created_at", "pk")
    if not activity.organization_id:
        related = related.filter(organization_reference=activity.organization_reference)
    related_rows = list(related[:31])
    return {
        "activity": result, "events": events,
        "last_event": ({
            "name": sanitize_text(last_event.name, max_length=160),
            "status": last_event.status,
            "summary": sanitize_text(last_event.summary, max_length=1000),
            "timestamp": last_event.timestamp,
        } if last_event else None),
        "metadata_display": json.dumps(result["metadata"], indent=2, ensure_ascii=False),
        "event_pagination": {"page": page.number, "pages": min(event_paginator.num_pages, 1000), "total": event_paginator.count,
                             "has_next": page.has_next() and page.number < 1000, "has_previous": page.has_previous(),
                             "truncated": event_paginator.num_pages > 1000},
        "related": [serialize_activity(item) for item in related_rows[:30]],
        "related_truncated": len(related_rows) > 30,
    }
