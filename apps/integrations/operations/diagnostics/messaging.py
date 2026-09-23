# ruff: noqa: F401
"""Conversation, AI and integration diagnostics for SHVYA support tooling."""

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
from apps.integrations.operations.diagnostics.common import (
    _safe_ai_markers,
    _safe_hosted_job,
    DiagnosticToolError,
    _iso,
    _tenant_safe_whatsapp_messages,
    _tenant_safe_instagram_conversations,
    _tenant_safe_instagram_messages,
    _tenant_safe_hosted_jobs,
    _tenant_safe_trigger_runs,
    _lead_for_org,
    _safe_lead,
)

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
