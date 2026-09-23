# ruff: noqa: F401
"""Lead and workspace diagnostics for SHVYA support tooling."""

from __future__ import annotations

import re
import uuid
from datetime import timedelta

from django.conf import settings
from django.db.models import BooleanField, Case, Count, F, Max, Q, Value, When
from django.utils import timezone

from apps.ai_engagement.services.confidentiality import (
    is_sensitive_attribute_definition,
)
from apps.ai_engagement.services.diagnostics import diagnose_engagement
from apps.channels.instagram_models import (
    InstagramAccount,
    InstagramConversation,
    InstagramMessage,
    InstagramWebhookDelivery,
)
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.analytics.models import AnalyticsSettings
from apps.crm.models import (
    AttributeDefinition,
    Lead,
    LeadCall,
    LeadNote,
    LeadReminder,
)
from apps.hosted_automation.models import HostedAutomationJob
from apps.integrations.diagnostic_auth import sanitize_data, sanitize_text
from apps.integrations.models import WebhookDelivery
from apps.triggers.models import TriggerEvent, TriggerRun
from services.channels.instagram_content import display_attachments

from apps.integrations.operations.diagnostics.common import (
    DiagnosticToolError,
    _iso,
    _tenant_safe_leads,
    _tenant_safe_whatsapp_messages,
    _tenant_safe_instagram_messages,
    _tenant_safe_hosted_jobs,
    _tenant_safe_trigger_runs,
    _lead_for_org,
    _safe_lead,
)

_SAFE_DIAGNOSTIC_CODE = re.compile(r"^[a-z0-9_:-]{1,80}$", re.IGNORECASE)

def get_workspace_profile(*, organization, arguments):
    return {
        "id": str(organization.id),
        "name": organization.name,
        "nickname": (
            f"{organization.name} — SHVYA diagnostics"
        ),
    }


def find_leads(*, organization, arguments):
    query = str(
        (arguments or {}).get("query") or ""
    ).strip()
    if not query:
        raise DiagnosticToolError("query is required.")
    if len(query) > 255:
        raise DiagnosticToolError(
            "query must be 255 characters or fewer."
        )

    try:
        limit = int((arguments or {}).get("limit") or 5)
    except (TypeError, ValueError):
        limit = 5
    limit = max(1, min(limit, 10))

    filters = (
        Q(name__icontains=query)
        | Q(phone__icontains=query)
        | Q(email__icontains=query)
    )
    try:
        filters |= Q(id=uuid.UUID(query))
    except (TypeError, ValueError, AttributeError):
        pass

    lead_qs = (
        _tenant_safe_leads(organization)
        .filter(filters)
        .select_related("pipeline", "stage")
    )
    match_count = lead_qs.count()
    leads = list(
        lead_qs.order_by("-updated_at")[:limit]
    )
    return {
        "matches": [_safe_lead(lead) for lead in leads],
        "count": len(leads),
        "match_count": match_count,
        "matches_returned": len(leads),
        "matches_truncated": match_count > len(leads),
    }


_AFFECTED_LEAD_ISSUES = {
    "qualification_completed_not_qualified",
    "workflow_failure",
    "hosted_ai_failure",
    "whatsapp_delivery_failure",
    "instagram_delivery_failure",
    "stalled_stage",
}


def find_affected_leads(*, organization, arguments):
    """Find a bounded tenant-scoped cohort sharing one persisted issue signal."""

    arguments = arguments or {}
    issue_type = str(
        arguments.get("issue_type") or ""
    ).strip().casefold()
    if issue_type not in _AFFECTED_LEAD_ISSUES:
        raise DiagnosticToolError(
            "issue_type must be one of: "
            + ", ".join(sorted(_AFFECTED_LEAD_ISSUES))
            + "."
        )

    try:
        days = int(arguments.get("days") or 30)
    except (TypeError, ValueError):
        days = 30
    days = max(1, min(days, 90))

    try:
        limit = int(arguments.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 50))

    pipeline_id = arguments.get("pipeline_id")
    stage_id = arguments.get("stage_id")
    lead_qs = _tenant_safe_leads(
        organization
    ).select_related("pipeline", "stage")

    if pipeline_id:
        try:
            parsed_pipeline_id = uuid.UUID(str(pipeline_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise DiagnosticToolError(
                "pipeline_id must be a valid UUID."
            ) from exc
        lead_qs = lead_qs.filter(
            pipeline_id=parsed_pipeline_id
        )

    if stage_id:
        try:
            parsed_stage_id = uuid.UUID(str(stage_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise DiagnosticToolError(
                "stage_id must be a valid UUID."
            ) from exc
        lead_qs = lead_qs.filter(
            stage_id=parsed_stage_id
        )

    now = timezone.now()
    since = now - timedelta(days=days)
    evidence = {
        "window_days": days,
        "pipeline_filter_applied": bool(pipeline_id),
        "stage_filter_applied": bool(stage_id),
    }

    if issue_type == "qualification_completed_not_qualified":
        lead_qs = lead_qs.filter(
            **{
                "attributes___shvya_ai_qualification__qualification_status": (
                    "completed"
                )
            }
        ).exclude(
            stage__name__iexact="Qualified"
        )
        evidence["semantics"] = (
            "Persisted qualification status is completed while the current "
            "stage is not named Qualified. Run diagnose_lead_qualification "
            "before applying any repair because completed does not by itself "
            "prove that qualification criteria are satisfied."
        )
    elif issue_type == "workflow_failure":
        affected_ids = _tenant_safe_trigger_runs(
            organization
        ).filter(
            status__in=["failed", "error"],
            created_at__gte=since,
        ).values("lead_id")
        lead_qs = lead_qs.filter(id__in=affected_ids)
    elif issue_type == "hosted_ai_failure":
        affected_ids = _tenant_safe_hosted_jobs(
            organization
        ).filter(
            status=HostedAutomationJob.Status.FAILED,
            created_at__gte=since,
        ).values("lead_id")
        lead_qs = lead_qs.filter(id__in=affected_ids)
    elif issue_type == "whatsapp_delivery_failure":
        affected_ids = _tenant_safe_whatsapp_messages(
            organization
        ).filter(
            lead__isnull=False,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            status=WhatsAppMessage.Status.FAILED,
            created_at__gte=since,
        ).values("lead_id")
        lead_qs = lead_qs.filter(id__in=affected_ids)
    elif issue_type == "instagram_delivery_failure":
        affected_ids = _tenant_safe_instagram_messages(
            organization
        ).filter(
            conversation__lead__isnull=False,
            direction=InstagramMessage.Direction.OUTBOUND,
            status=InstagramMessage.Status.FAILED,
            created_at__gte=since,
        ).values("conversation__lead_id")
        lead_qs = lead_qs.filter(id__in=affected_ids)
    else:
        settings_row = AnalyticsSettings.objects.filter(
            organization=organization
        ).only("stall_day_threshold").first()
        try:
            threshold_days = int(
                arguments.get("threshold_days")
                or getattr(settings_row, "stall_day_threshold", 7)
                or 7
            )
        except (TypeError, ValueError):
            threshold_days = 7
        threshold_days = max(1, min(threshold_days, 90))
        lead_qs = lead_qs.filter(
            stage_entered_at__lte=(
                now - timedelta(days=threshold_days)
            )
        )
        evidence["threshold_days"] = threshold_days

    lead_qs = lead_qs.distinct()
    match_count = lead_qs.count()
    leads = list(
        lead_qs.order_by(
            "-updated_at",
            "-id",
        )[:limit]
    )
    return {
        "issue_type": issue_type,
        "evidence": evidence,
        "matches": [_safe_lead(lead) for lead in leads],
        "match_count": match_count,
        "matches_returned": len(leads),
        "matches_truncated": match_count > len(leads),
    }


def _safe_lead_attributes(*, organization, attributes):
    values = attributes if isinstance(attributes, dict) else {}
    if not values:
        return {}, 0, 0

    definitions = {
        item.key: item
        for item in AttributeDefinition.objects.filter(
            organization=organization,
            key__in=list(values.keys()),
        ).only("key", "name")
    }
    safe = {}
    sensitive_redacted = 0
    undefined_omitted = 0
    for key, value in values.items():
        definition = definitions.get(str(key))
        if definition is None:
            # Orphan/legacy JSON has no current metadata proving what the value
            # represents. External diagnostics fail closed instead of exposing it.
            undefined_omitted += 1
            continue
        if is_sensitive_attribute_definition(
            {
                "key": definition.key,
                "name": definition.name,
            }
        ):
            sensitive_redacted += 1
            continue
        safe[str(key)] = value
    return (
        sanitize_data(safe),
        sensitive_redacted,
        undefined_omitted,
    )


def get_lead_snapshot(*, organization, arguments):
    lead = _lead_for_org(
        organization=organization,
        lead_id=(arguments or {}).get("lead_id"),
    )
    whatsapp = _tenant_safe_whatsapp_messages(
        organization
    ).filter(lead=lead)
    last_wa = whatsapp.order_by(
        "-created_at",
        "-id",
    ).first()

    instagram = _tenant_safe_instagram_messages(
        organization
    ).filter(conversation__lead=lead)
    last_ig = instagram.order_by(
        "-created_at",
        "-id",
    ).first()

    (
        safe_attributes,
        sensitive_attributes_redacted,
        undefined_attributes_omitted,
    ) = _safe_lead_attributes(
        organization=organization,
        attributes=lead.attributes or {},
    )
    result = _safe_lead(lead)
    result.update(
        {
            "attributes": safe_attributes,
            "sensitive_attributes_redacted": (
                sensitive_attributes_redacted
            ),
            "undefined_attributes_omitted": (
                undefined_attributes_omitted
            ),
            "notes_count": LeadNote.objects.filter(
                lead=lead
            ).count(),
            "calls_count": LeadCall.objects.filter(
                lead=lead
            ).count(),
            "reminders_count": LeadReminder.objects.filter(
                lead=lead
            ).count(),
            "whatsapp_message_count": whatsapp.count(),
            "instagram_message_count": instagram.count(),
            "last_whatsapp_activity_at": (
                _iso(last_wa.created_at)
                if last_wa
                else None
            ),
            "last_instagram_activity_at": (
                _iso(last_ig.created_at)
                if last_ig
                else None
            ),
            "pipeline_ai_enabled": bool(
                lead.pipeline.ai_enabled
            ),
            "stage_ai_enabled": bool(
                lead.stage.ai_on
            ),
            "stage_entered_at": _iso(
                lead.stage_entered_at
            ),
        }
    )
    return result
