"""Shared lead filtering used by CRM, WhatsApp inbox, and reminders."""

from datetime import date, timedelta
from urllib.parse import urlencode

from django.db.models import DateTimeField, F, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.crm.models import AttributeDefinition, Lead, Pipeline, Stage
from apps.crm.models.activity import LeadActivity


INTERNAL_ATTRIBUTE_KEYS = {"_shvya_ai_qualification"}
AI_QUALIFICATION_PREFIX = "<AI Qualification Summary"


def accessible_pipelines(user):
    """Return active pipelines the CRM user is allowed to see."""
    from apps.crm.views.api import get_user_pipelines

    return get_user_pipelines(user).filter(
        organization=user.organization,
        is_active=True,
    )


def public_attribute_definitions(organization):
    """Return only user-created/filterable attributes."""
    return (
        AttributeDefinition.objects.filter(organization=organization)
        .exclude(key__in=INTERNAL_ATTRIBUTE_KEYS)
        .order_by("display_order", "created_at")
    )


def _date_value(value):
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError:
        return None


def _days_value(value):
    try:
        days = int(str(value or "").strip())
    except (TypeError, ValueError):
        return None
    return days if days >= 0 else None


def _pipeline_entered_annotation(queryset):
    last_pipeline_change = (
        LeadActivity.objects.filter(
            lead_id=OuterRef("pk"),
            topic=LeadActivity.Topic.PIPELINE_CHANGED,
            new_pipeline_id=OuterRef("pipeline_id"),
        )
        .order_by("-created_at")
        .values("created_at")[:1]
    )
    return queryset.annotate(
        filter_pipeline_entered_at=Coalesce(
            Subquery(last_pipeline_change, output_field=DateTimeField()),
            F("created_at"),
        )
    )


def apply_lead_filters(queryset, params, *, user, include_search=False):
    """Apply the canonical CRM lead filters to any Lead queryset."""
    organization = user.organization
    allowed_pipelines = accessible_pipelines(user)

    if include_search:
        search = str(params.get("search") or "").strip()
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search)
                | Q(phone__icontains=search)
                | Q(email__icontains=search)
            )

    name = str(params.get("filter_name") or "").strip()
    phone = str(params.get("filter_phone") or "").strip()
    email = str(params.get("filter_email") or "").strip()
    notes = str(params.get("filter_notes") or "").strip()

    if name:
        queryset = queryset.filter(name__icontains=name)
    if phone:
        queryset = queryset.filter(phone__icontains=phone)
    if email:
        queryset = queryset.filter(email__icontains=email)
    if notes:
        queryset = queryset.filter(
            Q(notes__icontains=notes)
            | Q(lead_notes__note__icontains=notes)
        )

    pipeline_value = str(params.get("filter_pipeline") or "").strip()
    if pipeline_value and pipeline_value != "all":
        pipeline = allowed_pipelines.filter(id=pipeline_value).first()
        if pipeline:
            queryset = queryset.filter(pipeline=pipeline)
        else:
            return queryset.none()

    stage_value = str(params.get("filter_stage") or "").strip()
    if stage_value:
        stage = Stage.objects.filter(
            id=stage_value,
            pipeline__in=allowed_pipelines,
            is_active=True,
        ).first()
        if stage:
            queryset = queryset.filter(stage=stage)
        else:
            return queryset.none()

    allowed_attributes = {
        definition.key: definition
        for definition in public_attribute_definitions(organization)
    }
    for key, value in params.items():
        if not key.startswith("attr_"):
            continue
        value = str(value or "").strip()
        if not value:
            continue
        attribute_key = key[len("attr_"):]
        if attribute_key not in allowed_attributes:
            continue
        queryset = queryset.filter(
            **{f"attributes__{attribute_key}__icontains": value}
        )

    created_after = _date_value(params.get("filter_created_after"))
    created_before = _date_value(params.get("filter_created_before"))
    if created_after:
        queryset = queryset.filter(created_at__date__gte=created_after)
    if created_before:
        queryset = queryset.filter(created_at__date__lte=created_before)

    reminder_date = _date_value(params.get("filter_reminder_date"))
    if reminder_date:
        queryset = queryset.filter(
            reminders__status="pending",
            reminders__due_at__date=reminder_date,
        )

    ai_qualified_date = _date_value(params.get("filter_ai_qualified_date"))
    if ai_qualified_date:
        queryset = queryset.filter(
            lead_notes__note_type="system",
            lead_notes__note__startswith=AI_QUALIFICATION_PREFIX,
        ).filter(
            Q(lead_notes__created_at__date=ai_qualified_date)
            | Q(lead_notes__updated_at__date=ai_qualified_date)
        )

    days_in_stage = _days_value(params.get("filter_days_in_stage"))
    if days_in_stage is not None:
        cutoff = timezone.now() - timedelta(days=days_in_stage)
        queryset = queryset.filter(stage_entered_at__lte=cutoff)

    days_in_pipeline = _days_value(params.get("filter_days_in_pipeline"))
    if days_in_pipeline is not None:
        cutoff = timezone.now() - timedelta(days=days_in_pipeline)
        queryset = _pipeline_entered_annotation(queryset).filter(
            filter_pipeline_entered_at__lte=cutoff
        )

    return queryset.distinct()


def active_filter_items(params, *, user):
    """Build filter chips and per-chip removal query strings."""
    labels = {
        "filter_name": "Name",
        "filter_phone": "Phone",
        "filter_email": "Email",
        "filter_notes": "Notes",
        "filter_created_after": "Created from",
        "filter_created_before": "Created to",
        "filter_reminder_date": "Reminder date",
        "filter_ai_qualified_date": "AI qualified date",
        "filter_days_in_stage": "Days in stage",
        "filter_days_in_pipeline": "Days in pipeline",
    }
    definitions = {
        item.key: item.name
        for item in public_attribute_definitions(user.organization)
    }
    pipelines = {
        str(item.id): item.name
        for item in accessible_pipelines(user)
    }
    stages = {
        str(item.id): item.name
        for item in Stage.objects.filter(
            pipeline__in=accessible_pipelines(user),
            is_active=True,
        )
    }

    items = []
    for key, value in params.items():
        value = str(value or "").strip()
        if not value:
            continue

        label = labels.get(key)
        display_value = value
        if key == "filter_pipeline":
            label = "Pipeline"
            display_value = "All" if value == "all" else pipelines.get(value, value)
        elif key == "filter_stage":
            label = "Stage"
            display_value = stages.get(value, value)
        elif key.startswith("attr_"):
            attribute_key = key[len("attr_"):]
            if attribute_key not in definitions:
                continue
            label = definitions[attribute_key]

        if not label:
            continue

        query = params.copy()
        if key in query:
            query.pop(key)
        items.append(
            {
                "key": key,
                "label": label,
                "value": display_value,
                "remove_query": query.urlencode(),
            }
        )
    return items


def has_active_filters(params):
    return any(
        str(value or "").strip()
        for key, value in params.items()
        if key.startswith("filter_") or key.startswith("attr_")
    )


def cross_pipeline_matches(params, *, user, current_pipeline=None):
    """Return accessible pipelines containing matches for the active filters."""
    if not has_active_filters(params):
        return []

    clean_params = params.copy()
    clean_params.pop("filter_pipeline", None)
    matches = []
    for pipeline in accessible_pipelines(user):
        if current_pipeline and pipeline.id == current_pipeline.id:
            continue
        queryset = Lead.objects.filter(
            organization=user.organization,
            pipeline=pipeline,
        )
        queryset = apply_lead_filters(queryset, clean_params, user=user)
        count = queryset.count()
        if count:
            matches.append({"pipeline": pipeline, "count": count})
    return matches


def query_with(params, **changes):
    """Return a query string with selected values replaced."""
    query = params.copy()
    for key, value in changes.items():
        if value in (None, ""):
            query.pop(key, None)
        else:
            query[key] = str(value)
    return query.urlencode()
