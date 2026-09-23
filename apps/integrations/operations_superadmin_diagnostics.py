"""Additional safe Operations diagnostics.

These helpers are read-only. They expose deterministic policy testing, persisted
integration/webhook self-tests, and Superadmin-only configuration drift
comparison without returning cross-tenant configuration content.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import timedelta
from urllib.parse import urlparse

from django.db.models import Count, Max
from django.utils import timezone

from apps.ai_engagement.models import Document, FAQ, KnowledgeSource
from apps.ai_engagement.services.conversation_policy import (
    ConversationPolicyContext,
    ConversationPolicyEngine,
)
from apps.ai_engagement.services.intent_types import (
    ClassificationPath,
    Intent,
    IntentDecision,
)
from apps.channels.instagram_models import InstagramAccount
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import AttributeDefinition, Pipeline, Stage
from apps.followups.models import FollowupSequence
from apps.followups.touchpoint_models import TouchpointCategory, TouchpointReply
from apps.integrations.diagnostic_tools import (
    _tenant_safe_instagram_webhook_deliveries,
    get_integration_health,
)
from apps.integrations.models import WebhookConfiguration, WebhookDelivery
from apps.integrations.operations_policy import (
    CAP_DIAGNOSTICS_READ,
    ROLE_SUPERADMIN,
)
from apps.organizations.models import Organization
from apps.triggers.models import SmartTrigger
from services.channels.hosted_whatsapp_service import (
    get_pipeline_for_account,
    get_session_settings,
)

from apps.integrations.operations_tools import (
    OperationsPermissionError,
    OperationsToolError,
    ToolExecution,
    _organization_for,
    _require_operations_capability,
    _uuid,
)


def _hash(value) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def simulate_ai_response_policy(*, identity, arguments):
    """Run the canonical deterministic conversation policy with synthetic inputs."""

    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_DIAGNOSTICS_READ,
    )
    data = (arguments or {}).get("data")
    if not isinstance(data, dict):
        raise OperationsToolError("data must be an AI response-policy test object.")

    try:
        primary = Intent(str(data.get("primary_intent") or Intent.UNKNOWN.value))
    except ValueError as exc:
        raise OperationsToolError("Unknown primary_intent.") from exc

    secondary = []
    for raw in data.get("secondary_intents") or []:
        try:
            value = Intent(str(raw))
        except ValueError as exc:
            raise OperationsToolError(f"Unknown secondary intent: {raw}.") from exc
        if value != primary and value not in secondary:
            secondary.append(value)

    confidence = data.get("confidence", 1.0)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError) as exc:
        raise OperationsToolError("confidence must be a number between 0 and 1.") from exc
    if confidence < 0 or confidence > 1:
        raise OperationsToolError("confidence must be between 0 and 1.")

    direct_question = str(data.get("direct_question") or "").strip() or None
    requires_knowledge = data.get("requires_knowledge", False)
    ai_allowed = data.get("ai_allowed", True)
    continue_after_answer = data.get("continue_after_answer", False)
    qualification_accepted = data.get("qualification_accepted", False)
    if not all(
        isinstance(value, bool)
        for value in (
            requires_knowledge,
            ai_allowed,
            continue_after_answer,
            qualification_accepted,
        )
    ):
        raise OperationsToolError(
            "requires_knowledge, ai_allowed, continue_after_answer and "
            "qualification_accepted must be booleans."
        )

    capabilities = data.get("capabilities") or []
    if not isinstance(capabilities, list):
        raise OperationsToolError("capabilities must be a list.")
    allowed_capabilities = {"booking", "call"}
    capabilities = {
        str(value).strip()
        for value in capabilities
        if str(value).strip()
    }
    unknown = capabilities - allowed_capabilities
    if unknown:
        raise OperationsToolError(
            "Unsupported policy-test capabilities: " + ", ".join(sorted(unknown))
        )

    next_requirement_id = (
        str(data.get("next_requirement_id") or "").strip() or None
    )
    source_message_id = "simulation-source"
    qualification_result = (
        {
            "accepted": True,
            "source_message_id": source_message_id,
        }
        if qualification_accepted
        else {}
    )

    intent = IntentDecision(
        primary_intent=primary,
        secondary_intents=tuple(secondary),
        confidence=confidence,
        direct_question=direct_question,
        requires_knowledge=requires_knowledge,
        requires_human=(
            Intent.HUMAN_REQUEST in {primary, *secondary}
            or Intent.CALL_REQUEST in {primary, *secondary}
        ),
        classification_path=ClassificationPath.DETERMINISTIC,
        model="operations_policy_simulation",
    )
    decision = ConversationPolicyEngine().decide(
        ConversationPolicyContext(
            intent_decision=intent,
            organization_id=str(organization.id),
            lead_id="simulation",
            qualification_state={},
            qualification_result=qualification_result,
            next_requirement_id=next_requirement_id,
            knowledge_available=data.get("knowledge_available"),
            ai_allowed=ai_allowed,
            capabilities=frozenset(capabilities),
            channel=str(data.get("channel") or "whatsapp")[:32],
            continue_after_answer=continue_after_answer,
        )
    )
    result = decision.as_dict()
    # Debug reason is useful for tests but must remain a bounded backend
    # rationale, not model chain-of-thought.
    result["explanation_debug_reason"] = str(
        result.get("explanation_debug_reason") or ""
    )[:800]

    return ToolExecution(
        data={
            "simulation": True,
            "side_effects": False,
            "provider_calls": 0,
            "messages_sent": 0,
            "crm_mutations": 0,
            "input": {
                "primary_intent": primary.value,
                "secondary_intents": [item.value for item in secondary],
                "qualification_accepted": qualification_accepted,
                "next_requirement_present": bool(next_requirement_id),
                "ai_allowed": ai_allowed,
                "capabilities": sorted(capabilities),
            },
            "decision": result,
        },
        capability=CAP_DIAGNOSTICS_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={
            "simulation": "ai_response_policy",
            "primary_intent": primary.value,
            "outcome": decision.outcome.value,
            "reason_code": decision.reason_code,
        },
    )


def test_integration_runtime(*, identity, arguments):
    """Run a persisted-state integration self-test without provider network calls."""

    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_DIAGNOSTICS_READ,
    )
    channel = str((arguments or {}).get("channel") or "all").strip().lower()
    if channel not in {"all", "whatsapp", "instagram"}:
        raise OperationsToolError("channel must be all, whatsapp, or instagram.")

    health = get_integration_health(
        organization=organization,
        arguments={},
    )
    result = {
        "self_test": True,
        "network_calls": False,
        "side_effects": False,
        "messages_sent": 0,
    }

    if channel in {"all", "whatsapp"}:
        accounts = list(health.get("whatsapp") or [])
        routing = []
        blockers = []
        for account in (
            WhatsAppAccount.objects.filter(
                organization=organization,
                is_active=True,
            )
            .defer("access_token")
            .order_by("id")[:100]
        ):
            pipeline = get_pipeline_for_account(account=account)
            settings = get_session_settings(account=account)
            row = {
                "account_id": str(account.id),
                "connection_type": account.connection_type,
                "status": account.status,
                "pipeline_bound": pipeline is not None,
                "automation_settings_available": isinstance(settings, dict),
            }
            routing.append(row)
            if account.status != WhatsAppAccount.Status.CONNECTED:
                blockers.append(
                    {"account_id": str(account.id), "blocker": "account_not_connected"}
                )
            elif pipeline is None:
                blockers.append(
                    {"account_id": str(account.id), "blocker": "pipeline_routing_missing"}
                )
        if not accounts:
            blockers.append({"blocker": "no_whatsapp_account"})
        result["whatsapp"] = {
            "persisted_health": accounts,
            "account_count": health.get("whatsapp_count", len(accounts)),
            "routing": routing,
            "blockers": blockers,
            "passed": not blockers,
        }

    if channel in {"all", "instagram"}:
        instagram = health.get("instagram")
        if not instagram:
            result["instagram"] = {
                "configured": False,
                "passed": False,
                "blockers": ["no_instagram_account"],
            }
        else:
            chain = instagram.get("diagnostic_chain") or {}
            blocker = chain.get("first_known_ai_auto_reply_blocker")
            result["instagram"] = {
                "configured": True,
                "status": instagram.get("status"),
                "webhook_subscribed": instagram.get("webhook_subscribed"),
                "provider_auth_configured": instagram.get(
                    "provider_auth_configured"
                ),
                "provider_auth_expired": instagram.get(
                    "provider_auth_expired"
                ),
                "first_known_ai_auto_reply_blocker": blocker,
                "passed": blocker in {"ai_processing_runtime", None},
                "diagnostic_chain": chain,
            }

    return ToolExecution(
        data=result,
        capability=CAP_DIAGNOSTICS_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={
            "self_test": "integration_runtime",
            "channel": channel,
            "network_calls": False,
        },
    )


def _status_counts(queryset, field="status"):
    return {
        str(row[field]): int(row["count"])
        for row in queryset.values(field).annotate(count=Count("id")).order_by(field)
    }


def test_webhook_runtime(*, identity, arguments):
    """Inspect persisted webhook evidence without replaying or sending webhooks."""

    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_DIAGNOSTICS_READ,
    )
    try:
        hours = int((arguments or {}).get("hours") or 24)
    except (TypeError, ValueError) as exc:
        raise OperationsToolError("hours must be an integer.") from exc
    hours = max(1, min(hours, 168))
    since = timezone.now() - timedelta(hours=hours)

    webhook = WebhookConfiguration.objects.filter(
        organization=organization,
    ).first()
    deliveries = WebhookDelivery.objects.filter(
        organization=organization,
        created_at__gte=since,
    )
    latest = deliveries.order_by("-created_at").first()
    endpoint_host = ""
    if webhook and webhook.endpoint_url:
        endpoint_host = urlparse(webhook.endpoint_url).hostname or ""

    inbound_whatsapp = WhatsAppMessage.objects.filter(
        organization=organization,
        direction=WhatsAppMessage.Direction.INBOUND,
        created_at__gte=since,
    )
    latest_whatsapp = inbound_whatsapp.aggregate(value=Max("created_at"))["value"]

    instagram_account = InstagramAccount.objects.filter(
        organization=organization,
    ).first()
    instagram_deliveries = _tenant_safe_instagram_webhook_deliveries(
        organization
    ).filter(received_at__gte=since)
    latest_instagram = instagram_deliveries.order_by("-received_at").first()

    outbound_blockers = []
    if webhook is None:
        outbound_blockers.append("not_configured")
    else:
        if not webhook.is_enabled:
            outbound_blockers.append("disabled")
        if not webhook.endpoint_url:
            outbound_blockers.append("endpoint_missing")
        if not webhook.has_secret:
            outbound_blockers.append("signing_secret_missing")

    return ToolExecution(
        data={
            "self_test": True,
            "network_calls": False,
            "side_effects": False,
            "webhooks_sent": 0,
            "window_hours": hours,
            "outbound_lead_webhook": {
                "configured": webhook is not None,
                "enabled": bool(webhook and webhook.is_enabled),
                "endpoint_host": endpoint_host,
                "signing_secret_present": bool(webhook and webhook.has_secret),
                "delivery_count": deliveries.count(),
                "status_counts": _status_counts(deliveries),
                "latest": (
                    {
                        "status": latest.status,
                        "event_type": latest.event_type,
                        "response_status": latest.response_status,
                        "attempt_count": latest.attempt_count,
                        "delivered_at": (
                            latest.delivered_at.isoformat()
                            if latest.delivered_at
                            else None
                        ),
                        "has_error": bool(latest.error_message),
                    }
                    if latest
                    else None
                ),
                "blockers": outbound_blockers,
                "passed": not outbound_blockers,
            },
            "whatsapp_inbound_webhook_evidence": {
                "persisted_inbound_messages": inbound_whatsapp.count(),
                "last_inbound_at": (
                    latest_whatsapp.isoformat() if latest_whatsapp else None
                ),
                "evidence_available": inbound_whatsapp.exists(),
            },
            "instagram_webhook": {
                "account_configured": instagram_account is not None,
                "subscribed": bool(
                    instagram_account and instagram_account.webhook_subscribed
                ),
                "last_account_webhook_at": (
                    instagram_account.last_webhook_at.isoformat()
                    if instagram_account and instagram_account.last_webhook_at
                    else None
                ),
                "delivery_count": instagram_deliveries.count(),
                "status_counts": _status_counts(instagram_deliveries),
                "latest": (
                    {
                        "status": latest_instagram.status,
                        "received_at": latest_instagram.received_at.isoformat(),
                        "processed_at": (
                            latest_instagram.processed_at.isoformat()
                            if latest_instagram.processed_at
                            else None
                        ),
                        "has_error": bool(latest_instagram.error_message),
                    }
                    if latest_instagram
                    else None
                ),
            },
        },
        capability=CAP_DIAGNOSTICS_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={
            "self_test": "webhook_runtime",
            "window_hours": hours,
            "network_calls": False,
            "outbound_delivery_count": deliveries.count(),
            "instagram_delivery_count": instagram_deliveries.count(),
        },
    )


def _shape_for_org(organization):
    pipelines = list(
        Pipeline.objects.filter(organization=organization)
        .only("id", "is_active", "ai_enabled")
        .order_by("id")
    )
    pipeline_shapes = []
    for pipeline in pipelines:
        stages = list(
            Stage.objects.filter(pipeline=pipeline)
            .only("is_active", "ai_on", "is_system_locked", "display_order")
            .order_by("display_order", "id")
        )
        pipeline_shapes.append(
            {
                "active": pipeline.is_active,
                "ai_enabled": pipeline.ai_enabled,
                "stages": [
                    {
                        "active": stage.is_active,
                        "ai_on": stage.ai_on,
                        "protected": stage.is_system_locked,
                    }
                    for stage in stages
                ],
            }
        )

    attributes = sorted(
        [
            {
                "active": item.is_active,
                "field_type": item.field_type,
                "option_count": len(item.options or []),
            }
            for item in AttributeDefinition.objects.filter(
                organization=organization
            )
        ],
        key=lambda item: (
            str(item["field_type"]),
            int(item["option_count"]),
            bool(item["active"]),
        ),
    )

    workflows = sorted(
        [
            {
                "active": rule.is_active,
                "enabled": rule.enabled,
                "trigger_type": rule.trigger_type,
                "action_type": rule.action_type,
                "condition_keys": sorted(
                    (rule.conditions or {}).keys()
                    if isinstance(rule.conditions, dict)
                    else []
                ),
                "action_keys": sorted(
                    (rule.action or {}).keys()
                    if isinstance(rule.action, dict)
                    else []
                ),
            }
            for rule in SmartTrigger.objects.filter(organization=organization)
        ],
        key=lambda item: (
            str(item["trigger_type"]),
            str(item["action_type"]),
            json.dumps(item["condition_keys"]),
            json.dumps(item["action_keys"]),
        ),
    )

    cadences = []
    for sequence in (
        FollowupSequence.objects.filter(organization=organization)
        .select_related("whatsapp_account")
        .defer("whatsapp_account__access_token")
    ):
        cadences.append(
            {
                "active": sequence.is_active,
                "provider": sequence.whatsapp_account.connection_type,
                "steps": [
                    {
                        "type": step.step_type,
                        "schedule": step.schedule_type,
                        "active": step.is_active,
                    }
                    for step in sequence.steps.order_by("position", "id")
                ],
            }
        )
    cadences.sort(
        key=lambda item: (
            str(item["provider"]),
            json.dumps(item["steps"], sort_keys=True),
        )
    )

    accounts = sorted(
        [
            {
                "connection_type": account.connection_type,
                "status": account.status,
                "active": account.is_active,
                "pipeline_bound": get_pipeline_for_account(account=account) is not None,
            }
            for account in WhatsAppAccount.objects.filter(
                organization=organization
            ).defer("access_token")
        ],
        key=lambda item: (
            str(item["connection_type"]),
            str(item["status"]),
            bool(item["active"]),
        ),
    )

    categories = TouchpointCategory.objects.filter(organization=organization)
    touchpoint_shape = {
        "categories": categories.count(),
        "replies": TouchpointReply.objects.filter(
            category__organization=organization
        ).count(),
        "active_replies": TouchpointReply.objects.filter(
            category__organization=organization,
            is_active=True,
        ).count(),
    }

    docs = Document.objects.filter(organization=organization)
    knowledge = {
        "sources": KnowledgeSource.objects.filter(
            organization=organization
        ).count(),
        "active_sources": KnowledgeSource.objects.filter(
            organization=organization,
            is_active=True,
        ).count(),
        "documents": docs.count(),
        "active_documents": docs.filter(is_active=True).count(),
        "document_statuses": _status_counts(docs, "processing_status"),
    }

    faq_qs = FAQ.objects.filter(organization=organization)
    faq_shape = {
        "count": faq_qs.count(),
        "active": faq_qs.filter(is_active=True).count(),
    }

    from apps.integrations.operations_extended_tools import _qualification_public_snapshot

    qualification = _qualification_public_snapshot(organization)
    qualification_shape = {
        "mode": qualification.get("mode"),
        "requirement_count": len(qualification.get("requirements") or []),
        "mapping_target_count": sum(
            len(value)
            for value in (qualification.get("mappings") or {}).values()
        ),
        "completion_stage_configured": bool(
            qualification.get("completion_stage")
        ),
        "error_codes": sorted(
            str(item.get("code") or "")
            for item in (qualification.get("errors") or [])
        ),
    }

    return {
        "pipelines": pipeline_shapes,
        "attributes": attributes,
        "workflows": workflows,
        "cadences": cadences,
        "whatsapp": accounts,
        "qualification": qualification_shape,
        "touchpoints": touchpoint_shape,
        "faqs": faq_shape,
        "knowledge": knowledge,
    }


def _category_count(category, value):
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        if category == "qualification":
            return int(value.get("requirement_count") or 0)
        if category == "touchpoints":
            return int(value.get("replies") or 0)
        if category == "faqs":
            return int(value.get("count") or 0)
        if category == "knowledge":
            return int(value.get("documents") or 0)
    return 0


def compare_configuration_drift(*, identity, arguments):
    """Compare two tenant configurations server-side and return structure only."""

    selected = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=selected,
        capability=CAP_DIAGNOSTICS_READ,
    )
    if identity.role != ROLE_SUPERADMIN:
        raise OperationsPermissionError(
            "Configuration drift comparison is Superadmin-only."
        )

    reference_id = _uuid(
        (arguments or {}).get("reference_organization_id"),
        field="reference_organization_id",
    )
    if str(reference_id) == str(selected.id):
        raise OperationsToolError(
            "Reference organization must differ from the active organization."
        )
    reference = Organization.objects.filter(
        pk=reference_id,
        is_active=True,
    ).first()
    if reference is None:
        raise OperationsToolError("Active reference organization not found.")

    selected_shape = _shape_for_org(selected)
    reference_shape = _shape_for_org(reference)
    rows = []
    for category in sorted(selected_shape):
        left = selected_shape[category]
        right = reference_shape[category]
        left_hash = _hash(left)
        right_hash = _hash(right)
        rows.append(
            {
                "category": category,
                "matches": left_hash == right_hash,
                "selected_count": _category_count(category, left),
                "reference_count": _category_count(category, right),
                "selected_signature": left_hash,
                "reference_signature": right_hash,
            }
        )

    mismatches = [row["category"] for row in rows if not row["matches"]]
    return ToolExecution(
        data={
            "comparison": "structure_only",
            "tenant_content_exposed": False,
            "secret_material_exposed": False,
            "selected_context": "active_organization",
            "reference_context": "requested_organization",
            "overall_match": not mismatches,
            "mismatched_categories": mismatches,
            "categories": rows,
        },
        capability=CAP_DIAGNOSTICS_READ,
        target_type="organization_configuration_drift",
        target_id=str(selected.id),
        audit_summary={
            "comparison": "configuration_drift",
            "reference_organization_id": str(reference.id),
            "mismatch_count": len(mismatches),
            "tenant_content_exposed": False,
        },
    )


SUPERADMIN_DIAGNOSTIC_HANDLERS = {
    "simulate_ai_response_policy": simulate_ai_response_policy,
    "test_integration_runtime": test_integration_runtime,
    "test_webhook_runtime": test_webhook_runtime,
    "compare_configuration_drift": compare_configuration_drift,
}
