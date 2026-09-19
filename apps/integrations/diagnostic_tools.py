from __future__ import annotations

import uuid
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from apps.ai_engagement.services.diagnostics import diagnose_engagement
from apps.channels.instagram_models import InstagramAccount, InstagramMessage
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, LeadCall, LeadNote, LeadReminder
from apps.hosted_automation.models import HostedAutomationJob
from apps.integrations.diagnostic_auth import sanitize_data, sanitize_text
from apps.integrations.models import WebhookDelivery
from apps.triggers.models import TriggerEvent, TriggerRun


class DiagnosticToolError(ValueError):
    pass


def _iso(value):
    return value.isoformat() if value else None


def _lead_for_org(*, organization, lead_id):
    try:
        parsed = uuid.UUID(str(lead_id))
    except (TypeError, ValueError, AttributeError):
        raise DiagnosticToolError("lead_id must be a valid UUID.")

    lead = (
        Lead.objects.select_related(
            "organization",
            "pipeline",
            "stage",
        )
        .filter(pk=parsed, organization=organization)
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

    leads = list(
        Lead.objects.filter(organization=organization)
        .filter(filters)
        .select_related("pipeline", "stage")
        .order_by("-updated_at")[:limit]
    )
    return {
        "matches": [_safe_lead(lead) for lead in leads],
        "count": len(leads),
    }


def get_lead_snapshot(*, organization, arguments):
    lead = _lead_for_org(
        organization=organization,
        lead_id=(arguments or {}).get("lead_id"),
    )
    whatsapp = lead.whatsapp_messages.filter(
        organization=organization
    )
    last_wa = whatsapp.order_by(
        "-created_at",
        "-id",
    ).first()

    instagram = InstagramMessage.objects.filter(
        organization=organization,
        conversation__lead=lead,
        conversation__organization=organization,
    )
    last_ig = instagram.order_by(
        "-created_at",
        "-id",
    ).first()

    result = _safe_lead(lead)
    result.update(
        {
            "attributes": sanitize_data(
                lead.attributes or {}
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


def _safe_wa_message(message):
    return {
        "id": str(message.id),
        "external_id": message.external_id,
        "channel": "whatsapp",
        "connection_type": message.account.connection_type,
        "direction": message.direction,
        "status": message.status,
        "message_type": message.message_type,
        "from_number": message.from_number,
        "to_number": message.to_number,
        "body": sanitize_text(
            message.body,
            limit=1200,
            redact_long=False,
        ),
        "has_media": bool(message.media_payload),
        "has_error": bool(message.error),
        "created_at": _iso(message.created_at),
        "updated_at": _iso(message.updated_at),
    }


def _safe_instagram_message(message):
    attachments = (
        message.attachments
        if isinstance(message.attachments, list)
        else []
    )
    attachment_types = []
    for item in attachments[:10]:
        if isinstance(item, dict):
            attachment_types.append(
                str(
                    item.get("type")
                    or item.get("media_type")
                    or "attachment"
                )[:40]
            )
        else:
            attachment_types.append("attachment")

    return {
        "id": str(message.id),
        "external_id": message.external_id,
        "channel": "instagram",
        "direction": message.direction,
        "status": message.status,
        "message_type": message.message_type,
        "body": sanitize_text(
            message.body,
            limit=1200,
            redact_long=False,
        ),
        "attachment_count": len(attachments),
        "attachment_types": attachment_types,
        "has_error": bool(message.error),
        "created_at": _iso(message.created_at),
        "updated_at": _iso(message.updated_at),
    }


def get_conversation(*, organization, arguments):
    arguments = arguments or {}
    lead = _lead_for_org(
        organization=organization,
        lead_id=arguments.get("lead_id"),
    )
    channel = str(
        arguments.get("channel") or "all"
    ).strip().lower()
    if channel not in {
        "all",
        "whatsapp",
        "instagram",
    }:
        raise DiagnosticToolError(
            "channel must be all, whatsapp, or instagram."
        )

    try:
        limit = int(arguments.get("limit") or 30)
    except (TypeError, ValueError):
        limit = 30
    limit = max(1, min(limit, 50))

    rows = []

    if channel in {"all", "whatsapp"}:
        wa_rows = list(
            WhatsAppMessage.objects.filter(
                organization=organization,
                lead=lead,
            )
            .select_related("account")
            .order_by(
                "-created_at",
                "-id",
            )[:limit]
        )
        rows.extend(
            _safe_wa_message(item)
            for item in wa_rows
        )

    if channel in {"all", "instagram"}:
        ig_rows = list(
            InstagramMessage.objects.filter(
                organization=organization,
                conversation__lead=lead,
                conversation__organization=organization,
            )
            .select_related(
                "account",
                "conversation",
            )
            .order_by(
                "-created_at",
                "-id",
            )[:limit]
        )
        rows.extend(
            _safe_instagram_message(item)
            for item in ig_rows
        )

    rows.sort(
        key=lambda item: item.get("created_at") or ""
    )
    if len(rows) > limit:
        rows = rows[-limit:]

    return {
        "lead": _safe_lead(lead),
        "messages": rows,
        "count": len(rows),
    }


def trace_message(*, organization, arguments):
    identifier = str(
        (arguments or {}).get("message_id") or ""
    ).strip()
    if not identifier:
        raise DiagnosticToolError(
            "message_id is required."
        )

    wa_filters = Q(external_id=identifier)
    try:
        wa_filters |= Q(id=uuid.UUID(identifier))
    except (TypeError, ValueError, AttributeError):
        pass

    wa = (
        WhatsAppMessage.objects.filter(
            organization=organization
        )
        .filter(wa_filters)
        .select_related(
            "lead",
            "account",
        )
        .first()
    )

    if wa is not None:
        payload = (
            wa.raw_payload
            if isinstance(wa.raw_payload, dict)
            else {}
        )
        ai_markers = {
            key: payload.get(key)
            for key in (
                "shvya_ai_execution",
                "shvya_ai_processing",
                "shvya_ai",
            )
            if key in payload
        }

        hosted_job = (
            HostedAutomationJob.objects.filter(
                organization=organization,
                source_message=wa,
            )
            .order_by("-created_at")
            .first()
        )

        trigger_runs = list(
            TriggerRun.objects.filter(
                rule__organization=organization,
                message=wa,
            )
            .select_related("rule")
            .order_by("-created_at")[:20]
        )

        return sanitize_data(
            {
                "message": _safe_wa_message(wa),
                "lead_id": (
                    str(wa.lead_id)
                    if wa.lead_id
                    else None
                ),
                "ai_markers": ai_markers,
                "hosted_job": (
                    {
                        "id": str(hosted_job.id),
                        "status": hosted_job.status,
                        "available_at": _iso(
                            hosted_job.available_at
                        ),
                        "started_at": _iso(
                            hosted_job.started_at
                        ),
                        "completed_at": _iso(
                            hosted_job.completed_at
                        ),
                        "result": hosted_job.result,
                        "error": hosted_job.error,
                    }
                    if hosted_job
                    else None
                ),
                "workflow_runs": [
                    {
                        "id": str(run.id),
                        "rule": run.rule.name,
                        "action_type": run.action_type,
                        "status": run.status,
                        "detail": sanitize_text(
                            run.detail,
                            limit=500,
                        ),
                        "created_at": _iso(
                            run.created_at
                        ),
                        "finished_at": _iso(
                            run.finished_at
                        ),
                    }
                    for run in trigger_runs
                ],
            }
        )

    ig_filters = Q(external_id=identifier)
    try:
        ig_filters |= Q(id=uuid.UUID(identifier))
    except (TypeError, ValueError, AttributeError):
        pass

    ig = (
        InstagramMessage.objects.filter(
            organization=organization
        )
        .filter(ig_filters)
        .select_related(
            "conversation",
            "conversation__lead",
        )
        .first()
    )
    if ig is not None:
        return sanitize_data(
            {
                "message": _safe_instagram_message(ig),
                "conversation_id": str(
                    ig.conversation_id
                ),
                "lead_id": (
                    str(ig.conversation.lead_id)
                    if ig.conversation.lead_id
                    else None
                ),
            }
        )

    raise DiagnosticToolError(
        "Message not found in this organization."
    )


def get_ai_diagnostics(*, organization, arguments):
    lead = _lead_for_org(
        organization=organization,
        lead_id=(arguments or {}).get("lead_id"),
    )
    report = diagnose_engagement(lead=lead)
    report["lead"] = _safe_lead(lead)
    report["openai_configured"] = bool(
        getattr(
            settings,
            "OPENAI_API_KEY",
            "",
        ).strip()
    )
    return sanitize_data(report)


def get_integration_health(*, organization, arguments):
    whatsapp_accounts = list(
        WhatsAppAccount.objects.filter(
            organization=organization
        ).order_by("-updated_at")
    )
    instagram = InstagramAccount.objects.filter(
        organization=organization
    ).first()

    return {
        "whatsapp": [
            {
                "id": str(account.id),
                "connection_type": account.connection_type,
                "business_name": account.business_name,
                "display_phone_number": (
                    account.display_phone_number
                ),
                "phone_number_id_present": bool(
                    account.phone_number_id
                ),
                "waba_id_present": bool(account.waba_id),
                "credential_present": bool(
                    account.access_token
                ),
                "status": account.status,
                "is_active": account.is_active,
                "connected_at": _iso(
                    account.connected_at
                ),
                "updated_at": _iso(
                    account.updated_at
                ),
            }
            for account in whatsapp_accounts
        ],
        "instagram": (
            {
                "id": str(instagram.id),
                "username": instagram.username,
                "display_name": instagram.display_name,
                "status": instagram.status,
                "webhook_subscribed": (
                    instagram.webhook_subscribed
                ),
                "credential_present": bool(
                    instagram.access_token
                ),
                "last_webhook_at": _iso(
                    instagram.last_webhook_at
                ),
                "last_sync_at": _iso(
                    instagram.last_sync_at
                ),
                "has_last_error": bool(
                    instagram.last_error
                ),
                "updated_at": _iso(
                    instagram.updated_at
                ),
            }
            if instagram
            else None
        ),
    }


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

    events = list(
        TriggerEvent.objects.filter(
            organization=organization,
            lead=lead,
        ).order_by("-created_at")[:limit]
    )
    runs = list(
        TriggerRun.objects.filter(
            lead=lead,
            rule__organization=organization,
        )
        .select_related(
            "rule",
            "event",
        )
        .order_by("-created_at")[:limit]
    )

    return {
        "lead": _safe_lead(lead),
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

    whatsapp = list(
        WhatsAppMessage.objects.filter(
            organization=organization,
            status=WhatsAppMessage.Status.FAILED,
            created_at__gte=since,
        ).order_by("-created_at")[:limit]
    )
    instagram = list(
        InstagramMessage.objects.filter(
            organization=organization,
            status=InstagramMessage.Status.FAILED,
            created_at__gte=since,
        ).order_by("-created_at")[:limit]
    )
    hosted = list(
        HostedAutomationJob.objects.filter(
            organization=organization,
            status=HostedAutomationJob.Status.FAILED,
            created_at__gte=since,
        ).order_by("-created_at")[:limit]
    )
    webhooks = list(
        WebhookDelivery.objects.filter(
            organization=organization,
            status=WebhookDelivery.Status.FAILED,
            created_at__gte=since,
        ).order_by("-created_at")[:limit]
    )
    workflows = list(
        TriggerRun.objects.filter(
            rule__organization=organization,
            status__in=["failed", "error"],
            created_at__gte=since,
        )
        .select_related("rule")
        .order_by("-created_at")[:limit]
    )

    return sanitize_data(
        {
            "window_hours": hours,
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
            "hosted": [
                {
                    "job_id": str(item.id),
                    "lead_id": str(item.lead_id),
                    "status": item.status,
                    "error": item.error,
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
        HostedAutomationJob.objects.filter(
            organization=organization,
            status=HostedAutomationJob.Status.QUEUED,
            available_at__lte=stale_queued_before,
        ).count()
    )
    hosted_stale_processing = (
        HostedAutomationJob.objects.filter(
            organization=organization,
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
        "counts": {
            "leads": Lead.objects.filter(
                organization=organization
            ).count(),
            "whatsapp_connected": (
                WhatsAppAccount.objects.filter(
                    organization=organization,
                    status=WhatsAppAccount.Status.CONNECTED,
                    is_active=True,
                ).count()
            ),
            "whatsapp_inbound_24h": (
                WhatsAppMessage.objects.filter(
                    organization=organization,
                    direction=WhatsAppMessage.Direction.INBOUND,
                    created_at__gte=since,
                ).count()
            ),
            "whatsapp_failed_24h": (
                WhatsAppMessage.objects.filter(
                    organization=organization,
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
                InstagramMessage.objects.filter(
                    organization=organization,
                    direction=InstagramMessage.Direction.INBOUND,
                    created_at__gte=since,
                ).count()
            ),
            "instagram_failed_24h": (
                InstagramMessage.objects.filter(
                    organization=organization,
                    status=InstagramMessage.Status.FAILED,
                    created_at__gte=since,
                ).count()
            ),
            "hosted_failed_24h": (
                HostedAutomationJob.objects.filter(
                    organization=organization,
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
                TriggerRun.objects.filter(
                    rule__organization=organization,
                    status__in=["failed", "error"],
                    created_at__gte=since,
                ).count()
            ),
        },
    }


TOOL_HANDLERS = {
    "get_workspace_profile": get_workspace_profile,
    "find_leads": find_leads,
    "get_lead_snapshot": get_lead_snapshot,
    "get_conversation": get_conversation,
    "trace_message": trace_message,
    "get_ai_diagnostics": get_ai_diagnostics,
    "get_integration_health": get_integration_health,
    "get_workflow_trace": get_workflow_trace,
    "get_recent_errors": get_recent_errors,
    "get_runtime_health": get_runtime_health,
}


def execute_tool(
    *,
    name,
    organization,
    arguments,
):
    handler = TOOL_HANDLERS.get(
        str(name or "")
    )
    if handler is None:
        raise DiagnosticToolError(
            "Unknown diagnostic tool."
        )
    return handler(
        organization=organization,
        arguments=arguments or {},
    )
