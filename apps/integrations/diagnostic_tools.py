from __future__ import annotations

import re
import uuid
from datetime import timedelta

from django.conf import settings
from django.db.models import BooleanField, Case, F, Q, Value, When
from django.utils import timezone

from apps.ai_engagement.services.confidentiality import (
    is_sensitive_attribute_definition,
)
from apps.ai_engagement.services.diagnostics import diagnose_engagement
from apps.channels.instagram_models import (
    InstagramAccount,
    InstagramMessage,
    InstagramWebhookDelivery,
)
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
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


def _tenant_safe_instagram_messages(organization):
    safe_lead_ids = _tenant_safe_leads(
        organization
    ).values("id")
    return (
        InstagramMessage.objects.filter(
            organization=organization,
            account__organization=organization,
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
        _tenant_safe_leads(organization)
        .filter(filters)
        .select_related("pipeline", "stage")
        .order_by("-updated_at")[:limit]
    )
    return {
        "matches": [_safe_lead(lead) for lead in leads],
        "count": len(leads),
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
                sanitize_text(
                    item.get("type")
                    or item.get("media_type")
                    or "attachment",
                    limit=40,
                )
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
            _tenant_safe_whatsapp_messages(
                organization
            ).filter(lead=lead)
            .select_related("account")
            .defer("account__access_token")
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
            _tenant_safe_instagram_messages(
                organization
            ).filter(conversation__lead=lead)
            .select_related(
                "account",
                "conversation",
            )
            .defer("account__access_token")
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
        _tenant_safe_whatsapp_messages(
            organization
        )
        .filter(wa_filters)
        .select_related(
            "lead",
            "account",
        )
        .defer("account__access_token")
        .first()
    )

    if wa is not None:
        if wa.account.organization_id != organization.id:
            raise DiagnosticToolError(
                "Message tenant relationship mismatch detected."
            )
        if (
            wa.lead_id
            and wa.lead.organization_id != organization.id
        ):
            raise DiagnosticToolError(
                "Message tenant relationship mismatch detected."
            )
        payload = (
            wa.raw_payload
            if isinstance(wa.raw_payload, dict)
            else {}
        )
        ai_markers = _safe_ai_markers(payload)

        hosted_job = (
            _tenant_safe_hosted_jobs(
                organization
            ).filter(source_message=wa)
            .order_by("-created_at")
            .first()
        )

        trigger_runs = list(
            _tenant_safe_trigger_runs(
                organization
            ).filter(message=wa)
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
                "hosted_job": _safe_hosted_job(
                    hosted_job
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
        _tenant_safe_instagram_messages(
            organization
        )
        .filter(ig_filters)
        .select_related(
            "account",
            "conversation",
            "conversation__lead",
        )
        .defer("account__access_token")
        .first()
    )
    if ig is not None:
        if (
            ig.account.organization_id != organization.id
            or ig.conversation.organization_id
            != organization.id
            or (
                ig.conversation.lead_id
                and ig.conversation.lead.organization_id
                != organization.id
            )
        ):
            raise DiagnosticToolError(
                "Message tenant relationship mismatch detected."
            )
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
    whatsapp_qs = (
        WhatsAppAccount.objects.filter(
            organization=organization
        )
        .defer("access_token")
        .annotate(
            diagnostic_credential_present=Case(
                When(access_token="", then=Value(False)),
                default=Value(True),
                output_field=BooleanField(),
            )
        )
    )
    whatsapp_count = whatsapp_qs.count()
    whatsapp_accounts = list(
        whatsapp_qs.order_by("-updated_at")[:100]
    )
    instagram = (
        InstagramAccount.objects.filter(
            organization=organization
        )
        .defer("access_token")
        .annotate(
            diagnostic_credential_present=Case(
                When(access_token="", then=Value(False)),
                default=Value(True),
                output_field=BooleanField(),
            )
        )
        .first()
    )

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
                "provider_auth_configured": bool(
                    account.diagnostic_credential_present
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
        "whatsapp_count": whatsapp_count,
        "whatsapp_returned": len(
            whatsapp_accounts
        ),
        "whatsapp_truncated": (
            whatsapp_count > len(whatsapp_accounts)
        ),
        "instagram": (
            {
                "id": str(instagram.id),
                "username": instagram.username,
                "display_name": instagram.display_name,
                "status": instagram.status,
                "webhook_subscribed": (
                    instagram.webhook_subscribed
                ),
                "provider_auth_configured": bool(
                    instagram.diagnostic_credential_present
                ),
                "provider_auth_expires_at": _iso(
                    instagram.token_expires_at
                ),
                "provider_auth_refreshed_at": _iso(
                    instagram.token_refreshed_at
                ),
                "provider_auth_expired": bool(
                    instagram.token_expires_at
                    and instagram.token_expires_at <= timezone.now()
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
                "last_error": sanitize_text(
                    instagram.last_error,
                    limit=500,
                ) if instagram.last_error else "",
                "updated_at": _iso(
                    instagram.updated_at
                ),
                "automation_capabilities": {
                    "lead_linking": True,
                    "manual_outbound": True,
                    "ai_auto_reply_runtime": False,
                    "note": (
                        "This deployment has persisted Instagram webhook/inbox "
                        "and manual outbound support, but no Instagram AI "
                        "auto-reply execution runtime is exposed."
                    ),
                },
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
        _tenant_safe_trigger_events(
            organization
        ).filter(
            lead=lead,
        ).order_by("-created_at")[:limit]
    )
    runs = list(
        _tenant_safe_trigger_runs(
            organization
        ).filter(lead=lead)
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
        _tenant_safe_whatsapp_messages(
            organization
        ).filter(
            status=WhatsAppMessage.Status.FAILED,
            created_at__gte=since,
        )
        .order_by("-created_at")[:limit]
    )
    instagram = list(
        _tenant_safe_instagram_messages(
            organization
        ).filter(
            status=InstagramMessage.Status.FAILED,
            created_at__gte=since,
        )
        .order_by("-created_at")[:limit]
    )
    instagram_webhooks = list(
        InstagramWebhookDelivery.objects.filter(
            organization_ids__contains=[str(organization.id)],
            status=InstagramWebhookDelivery.Status.FAILED,
            received_at__gte=since,
        ).order_by("-received_at")[:limit]
    )
    hosted = list(
        _tenant_safe_hosted_jobs(
            organization
        ).filter(
            status=HostedAutomationJob.Status.FAILED,
            created_at__gte=since,
        )
        .order_by("-created_at")[:limit]
    )
    webhooks = list(
        _tenant_safe_webhook_deliveries(
            organization
        ).filter(
            status=WebhookDelivery.Status.FAILED,
            created_at__gte=since,
        ).order_by("-created_at")[:limit]
    )
    workflows = list(
        _tenant_safe_trigger_runs(
            organization
        ).filter(
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
                InstagramWebhookDelivery.objects.filter(
                    organization_ids__contains=[str(organization.id)],
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
