# ruff: noqa: F401
"""Workflow and runtime health diagnostics for SHVYA support tooling."""

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
    _safe_hosted_job,
    _iso,
    _tenant_safe_leads,
    _tenant_safe_whatsapp_messages,
    _tenant_safe_instagram_messages,
    _tenant_safe_instagram_webhook_deliveries,
    _tenant_safe_hosted_jobs,
    _tenant_safe_trigger_events,
    _tenant_safe_trigger_runs,
    _tenant_safe_webhook_deliveries,
    _lead_for_org,
    _safe_lead,
)

_SAFE_DIAGNOSTIC_CODE = re.compile(r"^[a-z0-9_:-]{1,80}$", re.IGNORECASE)

def get_workflow_trace(*, organization, arguments):
    lead = _lead_for_org(
        organization=organization,
        lead_id=(arguments or {}).get("lead_id"),
    )
    try:
        limit = int(
            (arguments or {}).get("limit") or 20
        )
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 50))

    event_qs = _tenant_safe_trigger_events(
        organization
    ).filter(
        lead=lead,
    )
    run_qs = (
        _tenant_safe_trigger_runs(
            organization
        ).filter(lead=lead)
        .select_related(
            "rule",
            "event",
        )
    )
    event_count = event_qs.count()
    run_count = run_qs.count()
    events = list(
        event_qs.order_by("-created_at")[:limit]
    )
    runs = list(
        run_qs.order_by("-created_at")[:limit]
    )

    first_problem = (
        run_qs.filter(
            status__in=["failed", "error", "blocked", "needs_review"],
        )
        .order_by("created_at", "id")
        .first()
    )
    if first_problem is not None:
        if (
            first_problem.action_type in {"message", "email"}
            and first_problem.status == "blocked"
        ):
            component = "delivery_policy"
        elif (
            first_problem.action_type in {"message", "email"}
            and first_problem.status == "needs_review"
        ):
            component = "delivery_outcome"
        else:
            component = "action_execution"
        first_failure = {
            "component": component,
            "run_id": str(first_problem.id),
            "rule_id": str(first_problem.rule_id),
            "rule_name": first_problem.rule.name,
            "action_type": first_problem.action_type,
            "status": first_problem.status,
            "reason": sanitize_text(
                first_problem.detail,
                limit=700,
            ),
            "created_at": _iso(first_problem.created_at),
        }
    elif event_count == 0:
        first_failure = {
            "component": "trigger_event",
            "reason": "No Workflow trigger event was recorded for this lead.",
        }
    elif run_count == 0:
        first_failure = {
            "component": "rule_match",
            "reason": (
                "Workflow events exist, but no Workflow run was created. "
                "Review trigger scope/conditions and eligibility."
            ),
        }
    else:
        first_failure = None

    return {
        "lead": _safe_lead(lead),
        "first_failure": first_failure,
        "events": [
            {
                "id": str(event.id),
                "kind": event.kind,
                "key": event.key,
                "created_at": _iso(
                    event.created_at
                ),
                "processed_at": _iso(
                    event.processed_at
                ),
            }
            for event in events
        ],
        "runs": [
            {
                "id": str(run.id),
                "event_id": str(run.event_id),
                "rule_id": str(run.rule_id),
                "rule_name": run.rule.name,
                "action_type": run.action_type,
                "status": run.status,
                "detail": sanitize_text(
                    run.detail,
                    limit=700,
                ),
                "due_at": _iso(run.due_at),
                "created_at": _iso(
                    run.created_at
                ),
                "finished_at": _iso(
                    run.finished_at
                ),
                "message_id": (
                    str(run.message_id)
                    if run.message_id
                    else None
                ),
            }
            for run in runs
        ],
        "event_count": event_count,
        "events_returned": len(events),
        "events_truncated": event_count > len(events),
        "run_count": run_count,
        "runs_returned": len(runs),
        "runs_truncated": run_count > len(runs),
    }


def get_recent_errors(*, organization, arguments):
    try:
        hours = int(
            (arguments or {}).get("hours") or 24
        )
    except (TypeError, ValueError):
        hours = 24
    hours = max(1, min(hours, 168))

    try:
        limit = int(
            (arguments or {}).get("limit") or 20
        )
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 50))

    since = timezone.now() - timedelta(
        hours=hours
    )

    whatsapp_qs = _tenant_safe_whatsapp_messages(
        organization
    ).filter(
        status=WhatsAppMessage.Status.FAILED,
        created_at__gte=since,
    )
    instagram_qs = _tenant_safe_instagram_messages(
        organization
    ).filter(
        status=InstagramMessage.Status.FAILED,
        created_at__gte=since,
    )
    instagram_webhook_qs = _tenant_safe_instagram_webhook_deliveries(
        organization
    ).filter(
        status=InstagramWebhookDelivery.Status.FAILED,
        received_at__gte=since,
    )
    hosted_qs = _tenant_safe_hosted_jobs(
        organization
    ).filter(
        status=HostedAutomationJob.Status.FAILED,
        created_at__gte=since,
    )
    webhook_qs = _tenant_safe_webhook_deliveries(
        organization
    ).filter(
        status=WebhookDelivery.Status.FAILED,
        created_at__gte=since,
    )
    workflow_qs = (
        _tenant_safe_trigger_runs(
            organization
        ).filter(
            status__in=["failed", "error"],
            created_at__gte=since,
        )
        .select_related("rule")
    )

    error_counts = {
        "whatsapp": whatsapp_qs.count(),
        "instagram": instagram_qs.count(),
        "instagram_webhooks": instagram_webhook_qs.count(),
        "hosted": hosted_qs.count(),
        "webhooks": webhook_qs.count(),
        "workflows": workflow_qs.count(),
    }
    whatsapp = list(
        whatsapp_qs.order_by("-created_at")[:limit]
    )
    instagram = list(
        instagram_qs.order_by("-created_at")[:limit]
    )
    instagram_webhooks = list(
        instagram_webhook_qs.order_by("-received_at")[:limit]
    )
    hosted = list(
        hosted_qs.order_by("-created_at")[:limit]
    )
    webhooks = list(
        webhook_qs.order_by("-created_at")[:limit]
    )
    workflows = list(
        workflow_qs.order_by("-created_at")[:limit]
    )

    return sanitize_data(
        {
            "window_hours": hours,
            "result_counts": {
                key: {
                    "total": error_counts[key],
                    "returned": len(rows),
                    "truncated": error_counts[key] > len(rows),
                }
                for key, rows in {
                    "whatsapp": whatsapp,
                    "instagram": instagram,
                    "instagram_webhooks": instagram_webhooks,
                    "hosted": hosted,
                    "webhooks": webhooks,
                    "workflows": workflows,
                }.items()
            },
            "whatsapp": [
                {
                    "message_id": str(item.id),
                    "external_id": item.external_id,
                    "lead_id": (
                        str(item.lead_id)
                        if item.lead_id
                        else None
                    ),
                    "error": item.error,
                    "created_at": _iso(
                        item.created_at
                    ),
                }
                for item in whatsapp
            ],
            "instagram": [
                {
                    "message_id": str(item.id),
                    "external_id": item.external_id,
                    "error": item.error,
                    "created_at": _iso(
                        item.created_at
                    ),
                }
                for item in instagram
            ],
            "instagram_webhooks": [
                {
                    "delivery_id": str(item.id),
                    "error": sanitize_text(
                        item.error_message,
                        limit=500,
                    ),
                    "received_at": _iso(
                        item.received_at
                    ),
                    "processed_at": _iso(
                        item.processed_at
                    ),
                }
                for item in instagram_webhooks
            ],
            "hosted": [
                {
                    "job_id": str(item.id),
                    "lead_id": str(item.lead_id),
                    "status": item.status,
                    "reason": _safe_hosted_job(
                        item
                    )["reason"],
                    "delivery_status": _safe_hosted_job(
                        item
                    )["delivery_status"],
                    "has_persisted_error": bool(
                        item.error
                    ),
                    "created_at": _iso(
                        item.created_at
                    ),
                }
                for item in hosted
            ],
            "webhooks": [
                {
                    "delivery_id": str(item.id),
                    "lead_id": str(item.lead_id),
                    "response_status": (
                        item.response_status
                    ),
                    "error": item.error_message,
                    "created_at": _iso(
                        item.created_at
                    ),
                }
                for item in webhooks
            ],
            "workflows": [
                {
                    "run_id": str(item.id),
                    "lead_id": str(item.lead_id),
                    "rule_name": item.rule.name,
                    "action_type": item.action_type,
                    "status": item.status,
                    "detail": item.detail,
                    "created_at": _iso(
                        item.created_at
                    ),
                }
                for item in workflows
            ],
        }
    )


def get_runtime_health(*, organization, arguments):
    now = timezone.now()
    since = now - timedelta(hours=24)
    stale_queued_before = now - timedelta(
        minutes=2
    )
    stale_processing_before = (
        now - timedelta(minutes=10)
    )

    hosted_stale_queued = (
        _tenant_safe_hosted_jobs(
            organization
        ).filter(
            status=HostedAutomationJob.Status.QUEUED,
            available_at__lte=stale_queued_before,
        ).count()
    )
    hosted_stale_processing = (
        _tenant_safe_hosted_jobs(
            organization
        ).filter(
            status=HostedAutomationJob.Status.PROCESSING,
            started_at__lte=stale_processing_before,
        ).count()
    )

    return {
        "organization_id": str(organization.id),
        "organization_name": organization.name,
        "organization_active": (
            organization.is_active
        ),
        "openai_configured": bool(
            getattr(
                settings,
                "OPENAI_API_KEY",
                "",
            ).strip()
        ),
        "celery_broker_configured": bool(
            getattr(
                settings,
                "CELERY_BROKER_URL",
                "",
            )
        ),
        "capabilities": {
            "instagram_ai_auto_reply_runtime": False,
        },
        "counts": {
            "leads": _tenant_safe_leads(
                organization
            ).count(),
            "whatsapp_connected": (
                WhatsAppAccount.objects.filter(
                    organization=organization,
                    status=WhatsAppAccount.Status.CONNECTED,
                    is_active=True,
                ).count()
            ),
            "whatsapp_inbound_24h": (
                _tenant_safe_whatsapp_messages(
                    organization
                ).filter(
                    direction=WhatsAppMessage.Direction.INBOUND,
                    created_at__gte=since,
                ).count()
            ),
            "whatsapp_failed_24h": (
                _tenant_safe_whatsapp_messages(
                    organization
                ).filter(
                    status=WhatsAppMessage.Status.FAILED,
                    created_at__gte=since,
                ).count()
            ),
            "instagram_connected": (
                InstagramAccount.objects.filter(
                    organization=organization,
                    status=InstagramAccount.Status.CONNECTED,
                ).count()
            ),
            "instagram_inbound_24h": (
                _tenant_safe_instagram_messages(
                    organization
                ).filter(
                    direction=InstagramMessage.Direction.INBOUND,
                    created_at__gte=since,
                ).count()
            ),
            "instagram_failed_24h": (
                _tenant_safe_instagram_messages(
                    organization
                ).filter(
                    status=InstagramMessage.Status.FAILED,
                    created_at__gte=since,
                ).count()
            ),
            "instagram_webhook_failed_24h": (
                _tenant_safe_instagram_webhook_deliveries(
                    organization
                ).filter(
                    status=InstagramWebhookDelivery.Status.FAILED,
                    received_at__gte=since,
                ).count()
            ),
            "hosted_failed_24h": (
                _tenant_safe_hosted_jobs(
                    organization
                ).filter(
                    status=HostedAutomationJob.Status.FAILED,
                    created_at__gte=since,
                ).count()
            ),
            "hosted_stale_queued": (
                hosted_stale_queued
            ),
            "hosted_stale_processing": (
                hosted_stale_processing
            ),
            "workflow_failed_24h": (
                _tenant_safe_trigger_runs(
                    organization
                ).filter(
                    status__in=["failed", "error"],
                    created_at__gte=since,
                ).count()
            ),
        },
    }
