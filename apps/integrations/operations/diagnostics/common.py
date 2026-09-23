# ruff: noqa: F401
"""Tenant-safe shared helpers for SHVYA diagnostic tooling."""

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

_SAFE_DIAGNOSTIC_CODE = re.compile(r"^[a-z0-9_:-]{1,80}$", re.IGNORECASE)

def _safe_diagnostic_code(value):
    value = str(value or "").strip()
    return value if _SAFE_DIAGNOSTIC_CODE.fullmatch(value) else ""


def _safe_ai_markers(payload):
    """Return only bounded AI execution linkage needed for support diagnosis."""

    payload = payload if isinstance(payload, dict) else {}
    execution = payload.get("shvya_ai_execution")
    execution = execution if isinstance(execution, dict) else {}
    processing = payload.get("shvya_ai_processing")
    processing = processing if isinstance(processing, dict) else {}
    ai_meta = payload.get("shvya_ai")
    ai_meta = ai_meta if isinstance(ai_meta, dict) else {}

    result = {}
    safe_execution = {
        key: execution.get(key)
        for key in (
            "status",
            "reason",
            "attempts",
            "updated_at",
        )
        if execution.get(key) is not None
    }
    if safe_execution:
        result["execution"] = safe_execution

    safe_processing = {
        key: processing.get(key)
        for key in (
            "processed",
            "message_id",
            "state_reconciled",
            "reconciled_at",
            "file_share_status",
        )
        if processing.get(key) is not None
    }
    if safe_processing:
        result["processing"] = safe_processing

    safe_ai_meta = {
        key: ai_meta.get(key)
        for key in (
            "source_inbound_message_id",
            "origin",
        )
        if ai_meta.get(key) is not None
    }
    if safe_ai_meta:
        result["outbound_linkage"] = safe_ai_meta

    return sanitize_data(result)


def _safe_hosted_job(job):
    if job is None:
        return None
    result = job.result if isinstance(job.result, dict) else {}
    delivery = result.get("delivery")
    return {
        "id": str(job.id),
        "status": job.status,
        "available_at": _iso(job.available_at),
        "started_at": _iso(job.started_at),
        "completed_at": _iso(job.completed_at),
        "reason": _safe_diagnostic_code(result.get("reason")),
        "delivery_status": (
            _safe_diagnostic_code(delivery.get("status"))
            if isinstance(delivery, dict)
            else ""
        ),
        "has_persisted_error": bool(job.error),
    }




class DiagnosticToolError(ValueError):
    pass


def _iso(value):
    return value.isoformat() if value else None


def _tenant_safe_leads(organization):
    return (
        Lead.objects.filter(
            organization=organization,
            pipeline__organization=organization,
            stage__pipeline__organization=organization,
        )
        .filter(stage__pipeline_id=F("pipeline_id"))
    )


def _tenant_safe_whatsapp_messages(organization):
    safe_lead_ids = _tenant_safe_leads(
        organization
    ).values("id")
    return (
        WhatsAppMessage.objects.filter(
            organization=organization,
            account__organization=organization,
        )
        .filter(
            Q(lead__isnull=True)
            | Q(lead_id__in=safe_lead_ids)
        )
    )


def _tenant_safe_instagram_conversations(organization):
    safe_lead_ids = _tenant_safe_leads(
        organization
    ).values("id")
    return (
        InstagramConversation.objects.filter(
            organization=organization,
            account__organization=organization,
        )
        .filter(
            Q(lead__isnull=True)
            | Q(lead_id__in=safe_lead_ids)
        )
    )


def _tenant_safe_instagram_messages(organization):
    safe_lead_ids = _tenant_safe_leads(
        organization
    ).values("id")
    safe_conversation_ids = _tenant_safe_instagram_conversations(
        organization
    ).values("id")
    return (
        InstagramMessage.objects.filter(
            organization=organization,
            account__organization=organization,
            conversation_id__in=safe_conversation_ids,
            conversation__organization=organization,
            conversation__account__organization=organization,
        )
        .filter(
            account_id=F("conversation__account_id"),
        )
        .filter(
            Q(conversation__lead__isnull=True)
            | Q(
                conversation__lead_id__in=safe_lead_ids
            )
        )
    )


def _tenant_safe_instagram_webhook_deliveries(organization):
    """Scope durable Instagram webhook failures by signed payload account id."""
    account_ids = list(
        InstagramAccount.objects.filter(
            organization=organization,
        ).values_list("ig_user_id", flat=True)
    )
    if not account_ids:
        return InstagramWebhookDelivery.objects.none()

    routing = Q()
    for account_id in account_ids:
        routing |= Q(
            raw_payload__entry__contains=[
                {"id": str(account_id)}
            ]
        )
    return InstagramWebhookDelivery.objects.filter(routing)


def _tenant_safe_hosted_jobs(organization):
    safe_lead_ids = _tenant_safe_leads(
        organization
    ).values("id")
    safe_message_ids = _tenant_safe_whatsapp_messages(
        organization
    ).values("id")
    return (
        HostedAutomationJob.objects.filter(
            organization=organization,
            account__organization=organization,
            lead_id__in=safe_lead_ids,
            source_message_id__in=safe_message_ids,
        )
        .filter(
            account_id=F("source_message__account_id"),
        )
        .filter(
            Q(source_message__lead__isnull=True)
            | Q(source_message__lead_id=F("lead_id"))
        )
    )


def _tenant_safe_trigger_events(organization):
    return TriggerEvent.objects.filter(
        organization=organization,
        lead_id__in=_tenant_safe_leads(
            organization
        ).values("id"),
    )


def _tenant_safe_trigger_runs(organization):
    safe_message_ids = _tenant_safe_whatsapp_messages(
        organization
    ).values("id")
    return (
        TriggerRun.objects.filter(
            rule__organization=organization,
            lead_id__in=_tenant_safe_leads(
                organization
            ).values("id"),
            event_id__in=_tenant_safe_trigger_events(
                organization
            ).values("id"),
        )
        .filter(
            lead_id=F("event__lead_id"),
        )
        .filter(
            Q(message__isnull=True)
            | Q(message_id__in=safe_message_ids)
        )
    )


def _tenant_safe_webhook_deliveries(organization):
    return WebhookDelivery.objects.filter(
        organization=organization,
        webhook__organization=organization,
        lead_id__in=_tenant_safe_leads(
            organization
        ).values("id"),
    )


def _lead_for_org(*, organization, lead_id):
    try:
        parsed = uuid.UUID(str(lead_id))
    except (TypeError, ValueError, AttributeError):
        raise DiagnosticToolError("lead_id must be a valid UUID.")

    lead = (
        _tenant_safe_leads(organization)
        .select_related(
            "organization",
            "pipeline",
            "stage",
        )
        .filter(pk=parsed)
        .first()
    )
    if lead is None:
        raise DiagnosticToolError(
            "Lead not found in this organization."
        )
    return lead


def _safe_lead(lead):
    return {
        "id": str(lead.id),
        "name": lead.name,
        "phone": lead.phone,
        "email": lead.email,
        "pipeline": {
            "id": str(lead.pipeline_id),
            "name": lead.pipeline.name,
        },
        "stage": {
            "id": str(lead.stage_id),
            "name": lead.stage.name,
        },
        "lead_source": lead.lead_source,
        "ai_enabled": lead.ai_enabled,
        "created_at": _iso(lead.created_at),
        "updated_at": _iso(lead.updated_at),
    }
