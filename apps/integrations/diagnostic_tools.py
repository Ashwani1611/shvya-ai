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

    displayed_media = display_attachments(
        attachments,
        message.raw_payload,
    )
    derived_media = []
    for item in displayed_media[:10]:
        derived_media.append(
            {
                "kind": sanitize_text(
                    item.get("kind"),
                    limit=40,
                ),
                "label": sanitize_text(
                    item.get("label"),
                    limit=80,
                ),
                "unavailable": bool(
                    item.get("unavailable")
                ),
            }
        )

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
        "attachment_types_returned": len(attachment_types),
        "attachment_types_truncated": (
            len(attachments) > len(attachment_types)
        ),
        "derived_media": derived_media,
        "derived_media_truncated": (
            len(displayed_media)
            > len(derived_media)
        ),
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
    whatsapp_count = 0
    instagram_count = 0

    if channel in {"all", "whatsapp"}:
        wa_qs = (
            _tenant_safe_whatsapp_messages(
                organization
            ).filter(lead=lead)
            .select_related("account")
            .defer("account__access_token")
        )
        whatsapp_count = wa_qs.count()
        wa_rows = list(
            wa_qs.order_by(
                "-created_at",
                "-id",
            )[:limit]
        )
        rows.extend(
            _safe_wa_message(item)
            for item in wa_rows
        )

    if channel in {"all", "instagram"}:
        ig_qs = (
            _tenant_safe_instagram_messages(
                organization
            ).filter(conversation__lead=lead)
            .select_related(
                "account",
                "conversation",
            )
            .defer("account__access_token")
        )
        instagram_count = ig_qs.count()
        ig_rows = list(
            ig_qs.order_by(
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

    message_count = whatsapp_count + instagram_count
    return {
        "lead": _safe_lead(lead),
        "messages": rows,
        "count": len(rows),
        "message_count": message_count,
        "messages_returned": len(rows),
        "messages_truncated": message_count > len(rows),
        "channel_counts": {
            "whatsapp": whatsapp_count,
            "instagram": instagram_count,
        },
    }


def trace_message(*, organization, arguments):
    identifier = str(
        (arguments or {}).get("message_id") or ""
    ).strip()
    if not identifier:
        raise DiagnosticToolError(
            "message_id is required."
        )
    if len(identifier) > 255:
        raise DiagnosticToolError(
            "message_id must be 255 characters or fewer."
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
                "provider_error": (
                    sanitize_text(
                        ig.error,
                        limit=500,
                    )
                    if ig.error
                    else ""
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


def _instagram_media_diagnostic_summary(messages):
    counts = {
        "story_reply": 0,
        "reel": 0,
        "shared_content": 0,
        "image": 0,
        "video": 0,
        "audio": 0,
        "other": 0,
    }
    observed = 0
    for message in messages:
        for item in display_attachments(
            message.attachments,
            message.raw_payload,
        ):
            observed += 1
            label = str(
                item.get("label") or ""
            ).casefold()
            kind = str(
                item.get("kind") or ""
            ).casefold()
            if "story" in label:
                counts["story_reply"] += 1
            elif "reel" in label:
                counts["reel"] += 1
            elif (
                "shared" in label
                or "post" in label
            ):
                counts["shared_content"] += 1
            elif kind in {
                "image",
                "video",
                "audio",
            }:
                counts[kind] += 1
            else:
                counts["other"] += 1
    return {
        "observed_items": observed,
        "type_counts": counts,
    }


def get_integration_health(*, organization, arguments):
    now = timezone.now()
    since = now - timedelta(hours=24)
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

    instagram_messages = _tenant_safe_instagram_messages(
        organization
    )
    instagram_conversations = (
        _tenant_safe_instagram_conversations(
            organization
        )
    )
    conversation_count = (
        instagram_conversations.count()
    )
    linked_conversation_count = (
        instagram_conversations.filter(
            lead__isnull=False
        ).count()
    )
    instagram_sourced_linked_leads = (
        instagram_conversations.filter(
            lead__isnull=False,
            lead__lead_source="instagram",
        )
        .values("lead_id")
        .distinct()
        .count()
    )
    linked_pipeline_qs = (
        instagram_conversations.filter(
            lead__isnull=False
        )
        .values(
            "lead__pipeline_id",
            "lead__pipeline__name",
        )
        .annotate(
            lead_count=Count(
                "lead_id",
                distinct=True,
            )
        )
        .order_by(
            "-lead_count",
            "lead__pipeline__name",
        )
    )
    linked_pipeline_group_count = (
        linked_pipeline_qs.count()
    )
    linked_pipeline_rows = list(
        linked_pipeline_qs[:20]
    )
    recent_instagram = instagram_messages.filter(
        created_at__gte=since
    )
    recent_message_count = recent_instagram.count()
    recent_inbound_count = recent_instagram.filter(
        direction=InstagramMessage.Direction.INBOUND
    ).count()
    recent_outbound_count = recent_instagram.filter(
        direction=InstagramMessage.Direction.OUTBOUND
    ).count()
    failed_outbound_qs = recent_instagram.filter(
        direction=InstagramMessage.Direction.OUTBOUND,
        status=InstagramMessage.Status.FAILED,
    )
    latest_failed_outbound = (
        failed_outbound_qs.order_by(
            "-created_at",
            "-id",
        ).first()
    )
    latest_inbound_at = instagram_messages.filter(
        direction=InstagramMessage.Direction.INBOUND
    ).aggregate(
        value=Max("created_at")
    )["value"]
    latest_outbound_at = instagram_messages.filter(
        direction=InstagramMessage.Direction.OUTBOUND
    ).aggregate(
        value=Max("created_at")
    )["value"]

    customer_started_conversations = (
        instagram_messages.filter(
            direction=InstagramMessage.Direction.INBOUND
        )
        .values("conversation_id")
        .distinct()
        .count()
    )
    standard_window_conversations = (
        instagram_messages.filter(
            direction=InstagramMessage.Direction.INBOUND,
        )
        .filter(
            Q(sent_at__gte=since)
            | Q(
                sent_at__isnull=True,
                created_at__gte=since,
            )
        )
        .values("conversation_id")
        .distinct()
        .count()
    )

    media_sample = list(
        recent_instagram.order_by(
            "-created_at",
            "-id",
        )[:100]
    )
    media_summary = (
        _instagram_media_diagnostic_summary(
            media_sample
        )
    )

    instagram_payload = None
    if instagram is not None:
        credential_expired = bool(
            instagram.token_expires_at
            and instagram.token_expires_at <= now
        )
        connected = (
            instagram.status
            == InstagramAccount.Status.CONNECTED
        )
        credential_ready = bool(
            instagram.diagnostic_credential_present
            and not credential_expired
        )
        eligible_now = (
            standard_window_conversations
            if connected and credential_ready
            else 0
        )
        if not connected:
            first_blocker = "connection"
        elif not credential_ready:
            first_blocker = "credential_health"
        elif not instagram.webhook_subscribed:
            first_blocker = "webhook"
        elif recent_inbound_count == 0:
            first_blocker = "incoming_message"
        else:
            first_blocker = "ai_processing_runtime"

        subscribed_fields = (
            instagram.subscribed_fields
            if isinstance(
                instagram.subscribed_fields,
                list,
            )
            else []
        )
        instagram_payload = {
            "id": str(instagram.id),
            "username": instagram.username,
            "display_name": instagram.display_name,
            "status": instagram.status,
            "webhook_subscribed": (
                instagram.webhook_subscribed
            ),
            "subscribed_webhook_fields": [
                sanitize_text(
                    item,
                    limit=80,
                )
                for item in subscribed_fields[:20]
            ],
            "subscribed_webhook_fields_truncated": (
                len(subscribed_fields) > 20
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
            "provider_auth_expired": credential_expired,
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
            "diagnostic_chain": {
                "connection": {
                    "status": instagram.status,
                    "connected": connected,
                },
                "credential_health": {
                    "configured": bool(
                        instagram.diagnostic_credential_present
                    ),
                    "expired": credential_expired,
                },
                "permissions": {
                    "grant_scope_list_persisted": False,
                    "verification_status": (
                        "unavailable_from_persisted_state"
                    ),
                    "note": (
                        "SHVYA does not persist Meta's granted-scope list, so "
                        "Operations does not claim permission verification from "
                        "connection state alone. Provider/OAuth errors and webhook "
                        "subscription evidence remain available for diagnosis."
                    ),
                },
                "webhook": {
                    "subscribed": bool(
                        instagram.webhook_subscribed
                    ),
                    "last_received_at": _iso(
                        instagram.last_webhook_at
                    ),
                },
                "message_event": {
                    "messages_24h": recent_message_count,
                    "inbound_24h": recent_inbound_count,
                    "outbound_24h": recent_outbound_count,
                    "last_inbound_at": _iso(
                        latest_inbound_at
                    ),
                    "last_outbound_at": _iso(
                        latest_outbound_at
                    ),
                },
                "media_story_reel_handling": {
                    "normalization_supported": True,
                    "sample_messages": len(
                        media_sample
                    ),
                    "sample_truncated": (
                        recent_message_count
                        > len(media_sample)
                    ),
                    **media_summary,
                },
                "lead_creation_and_linking": {
                    "conversation_count": conversation_count,
                    "linked_to_lead": (
                        linked_conversation_count
                    ),
                    "instagram_sourced_linked_leads": (
                        instagram_sourced_linked_leads
                    ),
                    "unlinked": (
                        conversation_count
                        - linked_conversation_count
                    ),
                },
                "pipeline_mapping": {
                    "linked_leads_tenant_validated": True,
                    "linked_conversations": (
                        linked_conversation_count
                    ),
                    "pipelines": [
                        {
                            "pipeline_id": str(
                                row["lead__pipeline_id"]
                            ),
                            "pipeline": (
                                row["lead__pipeline__name"]
                            ),
                            "lead_count": row["lead_count"],
                        }
                        for row in linked_pipeline_rows
                    ],
                    "pipeline_group_count": (
                        linked_pipeline_group_count
                    ),
                    "pipelines_returned": len(
                        linked_pipeline_rows
                    ),
                    "pipelines_truncated": (
                        linked_pipeline_group_count
                        > len(linked_pipeline_rows)
                    ),
                },
                "ai_processing": {
                    "runtime_available": False,
                    "status": "not_exposed",
                    "reason": (
                        "Persisted Instagram inbox and manual outbound "
                        "messaging are available, but this deployment "
                        "does not expose an Instagram AI auto-reply "
                        "execution runtime."
                    ),
                },
                "outbound_eligibility": {
                    "customer_started_conversations": (
                        customer_started_conversations
                    ),
                    "standard_window_hours": 24,
                    "standard_window_conversations": (
                        standard_window_conversations
                    ),
                    "eligible_now": eligible_now,
                    "human_agent_enabled": False,
                },
                "provider_response": {
                    "failed_outbound_24h": (
                        failed_outbound_qs.count()
                    ),
                    "last_failed_at": (
                        _iso(
                            latest_failed_outbound.created_at
                        )
                        if latest_failed_outbound
                        else None
                    ),
                    "last_failed_error": (
                        sanitize_text(
                            latest_failed_outbound.error,
                            limit=500,
                        )
                        if latest_failed_outbound
                        and latest_failed_outbound.error
                        else ""
                    ),
                },
                "first_known_ai_auto_reply_blocker": (
                    first_blocker
                ),
            },
            "automation_capabilities": {
                "lead_linking": True,
                "manual_outbound": True,
                "media_story_reel_normalization": True,
                "ai_auto_reply_runtime": False,
                "note": (
                    "Instagram diagnostics now cover connection, credential "
                    "health, webhook, persisted messages, media/story/reel "
                    "normalization, CRM lead linking, reply-window eligibility "
                    "and provider failures. AI auto-reply execution itself is "
                    "not exposed by this deployment."
                ),
            },
        }

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
        "instagram": instagram_payload,
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


TOOL_HANDLERS = {
    "get_workspace_profile": get_workspace_profile,
    "find_leads": find_leads,
    "find_affected_leads": find_affected_leads,
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
