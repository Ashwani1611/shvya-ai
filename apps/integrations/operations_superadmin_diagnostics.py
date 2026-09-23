"""Additional safe Operations diagnostics for Superadmin and organization admins."""

from __future__ import annotations

import hashlib
import json
from collections import Counter

from django.core.exceptions import ValidationError
from django.db.models import Count
from django.utils import timezone

from apps.ai_engagement.models import OrgInfo
from apps.ai_engagement.services.engagement_instruction_policy import (
    compile_engagement_instruction_policy,
)
from apps.channels.instagram_models import InstagramAccount
from apps.channels.models import WhatsAppAccount
from apps.crm.models import AttributeDefinition, Pipeline, Stage
from apps.followups.models import FollowupSequence
from apps.integrations.models import (
    EmailConfiguration,
    GoogleSheetIntegration,
    WebhookConfiguration,
    WebhookDelivery,
)
from apps.integrations.operations_policy import (
    CAP_DIAGNOSTICS_READ,
    ROLE_SUPERADMIN,
)
from apps.organizations.models import Organization
from apps.triggers.models import SmartTrigger
from services.channels.hosted_whatsapp_service import get_pipeline_for_account
from services.triggers.rules import validate as validate_workflow_rule

from apps.integrations.operations_tools import (
    OperationsPermissionError,
    OperationsToolError,
    ToolExecution,
    _organization_for,
    _require_operations_capability,
    _uuid,
)


def _digest(value) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _first_failure(checks):
    for item in checks:
        if not item["ok"]:
            return {
                "component": item["component"],
                "reason": item["reason"],
            }
    return None


def _select_whatsapp_account(organization, raw_id):
    qs = WhatsAppAccount.objects.filter(
        organization=organization,
        is_active=True,
    ).defer("access_token")
    if raw_id:
        account = qs.filter(pk=_uuid(raw_id, field="resource_id")).first()
        if account is None:
            raise OperationsToolError(
                "Active WhatsApp account not found in this organization."
            )
        return account
    rows = list(qs.order_by("business_name", "display_phone_number", "id")[:2])
    if not rows:
        raise OperationsToolError(
            "No active WhatsApp account is configured for this organization."
        )
    if len(rows) > 1:
        raise OperationsToolError(
            "Multiple WhatsApp accounts exist; provide resource_id."
        )
    return rows[0]


def _select_google_sheet(organization, raw_id):
    qs = GoogleSheetIntegration.objects.filter(organization=organization)
    if raw_id:
        item = qs.filter(pk=_uuid(raw_id, field="resource_id")).first()
        if item is None:
            raise OperationsToolError(
                "Google Sheets integration not found in this organization."
            )
        return item
    rows = list(qs.order_by("-is_enabled", "-updated_at", "id")[:2])
    if not rows:
        raise OperationsToolError(
            "No Google Sheets integration is configured for this organization."
        )
    if len(rows) > 1:
        raise OperationsToolError(
            "Multiple Google Sheets integrations exist; provide resource_id."
        )
    return rows[0]


def test_integration_connection(*, identity, arguments):
    """Run bounded readiness/live-safe checks without sending customer content."""
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_DIAGNOSTICS_READ,
    )
    integration = str((arguments or {}).get("integration") or "").strip().lower()
    resource_id = str((arguments or {}).get("resource_id") or "").strip()
    live = (arguments or {}).get("live", False)
    if not isinstance(live, bool):
        raise OperationsToolError("live must be a boolean.")

    if integration == "email":
        configuration = EmailConfiguration.objects.filter(
            organization=organization,
        ).first()
        if configuration is None:
            raise OperationsToolError(
                "No email configuration exists for this organization."
            )
        checks = [
            {
                "component": "configuration",
                "ok": bool(
                    configuration.smtp_host
                    and configuration.smtp_username
                    and configuration.email_address
                ),
                "reason": "SMTP host, username, or sender email is missing.",
            },
            {
                "component": "credential",
                "ok": configuration.has_password,
                "reason": "SMTP password/app password is not configured.",
            },
        ]
        live_result = "not_requested"
        if live and all(item["ok"] for item in checks):
            from apps.integrations.services.email import (
                EmailConfigurationError,
                test_email_configuration,
            )

            try:
                test_email_configuration(configuration)
                live_result = "passed"
            except EmailConfigurationError as exc:
                live_result = "failed"
                checks.append(
                    {
                        "component": "provider_authentication",
                        "ok": False,
                        "reason": str(exc)[:500],
                    }
                )
        return ToolExecution(
            data={
                "integration": "email",
                "ready": all(item["ok"] for item in checks),
                "live_test": live_result,
                "checks": checks,
                "first_failure": _first_failure(checks),
                "side_effects": False,
                "messages_sent": 0,
                "sensitive_values_returned": False,
            },
            capability=CAP_DIAGNOSTICS_READ,
            target_type="integration",
            target_id=str(configuration.id),
            audit_summary={
                "integration": "email",
                "live": live,
                "passed": all(item["ok"] for item in checks),
            },
        )

    if integration == "whatsapp":
        account = _select_whatsapp_account(organization, resource_id)
        pipeline = get_pipeline_for_account(account=account)
        checks = [
            {
                "component": "connection",
                "ok": account.status == WhatsAppAccount.Status.CONNECTED,
                "reason": "WhatsApp account is not connected.",
            },
            {
                "component": "pipeline_routing",
                "ok": pipeline is not None,
                "reason": "WhatsApp account is not bound to exactly one active pipeline.",
            },
        ]
        if account.connection_type == WhatsAppAccount.ConnectionType.API:
            # Access-token value is never returned. Reading the encrypted field
            # here is only a local readiness check.
            checks.append(
                {
                    "component": "credential",
                    "ok": bool(account.access_token),
                    "reason": "WhatsApp API credential is not configured.",
                }
            )
        return ToolExecution(
            data={
                "integration": "whatsapp",
                "ready": all(item["ok"] for item in checks),
                "connection_type": account.connection_type,
                "status": account.status,
                "pipeline_id": str(pipeline.id) if pipeline else None,
                "checks": checks,
                "first_failure": _first_failure(checks),
                "live_test": "readiness_only",
                "side_effects": False,
                "messages_sent": 0,
                "sensitive_values_returned": False,
            },
            capability=CAP_DIAGNOSTICS_READ,
            target_type="whatsapp_account",
            target_id=str(account.id),
            audit_summary={
                "integration": "whatsapp",
                "passed": all(item["ok"] for item in checks),
                "connection_type": account.connection_type,
            },
        )

    if integration == "instagram":
        account = InstagramAccount.objects.filter(
            organization=organization,
        ).first()
        if account is None:
            raise OperationsToolError(
                "No Instagram account is configured for this organization."
            )
        now = timezone.now()
        credential_present = bool(account.access_token)
        credential_expired = bool(
            account.token_expires_at and account.token_expires_at <= now
        )
        checks = [
            {
                "component": "connection",
                "ok": account.status == InstagramAccount.Status.CONNECTED,
                "reason": "Instagram account is not connected.",
            },
            {
                "component": "credential",
                "ok": credential_present and not credential_expired,
                "reason": "Instagram credential is missing or expired.",
            },
            {
                "component": "webhook_subscription",
                "ok": bool(account.webhook_subscribed),
                "reason": "Instagram webhook subscription is not active.",
            },
        ]
        live_result = "not_requested"
        if live and checks[0]["ok"] and checks[1]["ok"]:
            from services.channels.instagram_service import (
                InstagramAPIError,
                _fetch_profile,
            )

            try:
                _fetch_profile(account.access_token)
                live_result = "passed"
            except InstagramAPIError as exc:
                live_result = "failed"
                checks.append(
                    {
                        "component": "provider_profile_read",
                        "ok": False,
                        "reason": str(exc)[:500],
                    }
                )
        return ToolExecution(
            data={
                "integration": "instagram",
                "ready": all(item["ok"] for item in checks),
                "live_test": live_result,
                "checks": checks,
                "first_failure": _first_failure(checks),
                "side_effects": False,
                "messages_sent": 0,
                "sensitive_values_returned": False,
            },
            capability=CAP_DIAGNOSTICS_READ,
            target_type="instagram_account",
            target_id=str(account.id),
            audit_summary={
                "integration": "instagram",
                "live": live,
                "passed": all(item["ok"] for item in checks),
            },
        )

    if integration == "google_sheets":
        item = _select_google_sheet(organization, resource_id)
        checks = [
            {
                "component": "configuration",
                "ok": bool(item.spreadsheet_id and item.worksheet_name),
                "reason": "Spreadsheet or worksheet identity is missing.",
            },
            {
                "component": "webhook_secret",
                "ok": item.has_secret,
                "reason": "Google Sheets webhook secret is not configured.",
            },
            {
                "component": "pipeline",
                "ok": bool(item.pipeline_id and item.pipeline.is_active),
                "reason": "Mapped pipeline is missing or inactive.",
            },
            {
                "component": "stage",
                "ok": bool(
                    item.stage_id
                    and item.stage.is_active
                    and item.stage.pipeline_id == item.pipeline_id
                ),
                "reason": "Mapped stage is missing, inactive, or belongs to another pipeline.",
            },
            {
                "component": "mapping",
                "ok": bool(item.mapping),
                "reason": "No Google Sheets field mapping is configured.",
            },
        ]
        return ToolExecution(
            data={
                "integration": "google_sheets",
                "ready": all(check["ok"] for check in checks),
                "enabled": item.is_enabled,
                "registered": bool(item.last_registered_at),
                "last_sync_at": (
                    item.last_synced_at.isoformat()
                    if item.last_synced_at
                    else None
                ),
                "has_last_error": bool(item.last_error),
                "checks": checks,
                "first_failure": _first_failure(checks),
                "live_test": "readiness_only",
                "side_effects": False,
                "messages_sent": 0,
                "sensitive_values_returned": False,
            },
            capability=CAP_DIAGNOSTICS_READ,
            target_type="google_sheet_integration",
            target_id=str(item.id),
            audit_summary={
                "integration": "google_sheets",
                "passed": all(check["ok"] for check in checks),
            },
        )

    if integration == "webhook":
        configuration = WebhookConfiguration.objects.filter(
            organization=organization,
        ).first()
        if configuration is None:
            raise OperationsToolError(
                "No outbound webhook configuration exists for this organization."
            )
        checks = [
            {
                "component": "configuration",
                "ok": bool(configuration.endpoint_url),
                "reason": "Webhook endpoint URL is missing.",
            },
            {
                "component": "webhook_secret",
                "ok": configuration.has_secret,
                "reason": "Webhook signing secret is not configured.",
            },
        ]
        if configuration.endpoint_url:
            from apps.integrations.services.webhook import assert_public_webhook_target
            from django.core.exceptions import ValidationError

            try:
                assert_public_webhook_target(configuration.endpoint_url)
                checks.append(
                    {
                        "component": "public_target",
                        "ok": True,
                        "reason": "",
                    }
                )
            except ValidationError as exc:
                checks.append(
                    {
                        "component": "public_target",
                        "ok": False,
                        "reason": "; ".join(exc.messages)[:500],
                    }
                )
        recent = WebhookDelivery.objects.filter(
            organization=organization,
            webhook=configuration,
        )
        status_counts = {
            row["status"]: row["count"]
            for row in recent.values("status").annotate(count=Count("id"))
        }
        return ToolExecution(
            data={
                "integration": "webhook",
                "ready": all(check["ok"] for check in checks),
                "enabled": configuration.is_enabled,
                "checks": checks,
                "first_failure": _first_failure(checks),
                "delivery_counts": status_counts,
                "live_test": "target_validation_only",
                "side_effects": False,
                "webhook_requests_sent": 0,
                "sensitive_values_returned": False,
            },
            capability=CAP_DIAGNOSTICS_READ,
            target_type="webhook_configuration",
            target_id=str(configuration.id),
            audit_summary={
                "integration": "webhook",
                "passed": all(check["ok"] for check in checks),
                "delivery_count": sum(status_counts.values()),
            },
        )

    raise OperationsToolError(
        "integration must be email, whatsapp, instagram, google_sheets, or webhook."
    )


def _normalize_config_for_compare(value):
    drop_keys = {
        "source_id",
        "organization_id",
        "display_phone_number",
        "phone_number",
        "account_ref",
        "pipeline_id",
        "stage_id",
        "whatsapp_account_id",
    }
    if isinstance(value, dict):
        return {
            key: _normalize_config_for_compare(item)
            for key, item in sorted(value.items())
            if key not in drop_keys and not key.endswith("_id")
        }
    if isinstance(value, list):
        return [_normalize_config_for_compare(item) for item in value]
    return value


def _section_multiset(value):
    if isinstance(value, list):
        return Counter(_digest(_normalize_config_for_compare(item)) for item in value)
    return Counter({_digest(_normalize_config_for_compare(value)): 1})


def compare_organization_configuration(*, identity, arguments):
    """Compare two tenants by opaque configuration fingerprints only."""
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_DIAGNOSTICS_READ,
    )
    if identity.role != ROLE_SUPERADMIN:
        raise OperationsPermissionError(
            "Configuration drift comparison between organizations is Superadmin-only."
        )
    target_id = _uuid(
        (arguments or {}).get("target_organization_id"),
        field="target_organization_id",
    )
    target = Organization.objects.filter(pk=target_id, is_active=True).first()
    if target is None:
        raise OperationsToolError("Active target organization not found.")
    if target.id == organization.id:
        raise OperationsToolError(
            "Choose a different target organization for drift comparison."
        )

    from apps.integrations.operations_configuration_management import (
        _portable_configuration,
    )

    current = _portable_configuration(organization)
    other = _portable_configuration(target)
    sections = [
        "ai",
        "pipelines",
        "attributes",
        "cadences",
        "workflows",
        "touchpoints",
        "faqs",
        "messaging",
    ]
    rows = []
    for section in sections:
        left = _section_multiset(current.get(section, []))
        right = _section_multiset(other.get(section, []))
        exact = sum((left & right).values())
        left_count = sum(left.values())
        right_count = sum(right.values())
        rows.append(
            {
                "section": section,
                "current_count": left_count,
                "target_count": right_count,
                "exact_match_count": exact,
                "current_only_count": left_count - exact,
                "target_only_count": right_count - exact,
                "same": left == right,
                "current_digest": _digest(sorted(left.elements()))[:20],
                "target_digest": _digest(sorted(right.elements()))[:20],
            }
        )
    same = all(row["same"] for row in rows)
    return ToolExecution(
        data={
            "same_configuration": same,
            "current_organization_id": str(organization.id),
            "target_organization_id": str(target.id),
            "sections": rows,
            "raw_configuration_exposed": False,
            "customer_records_compared": 0,
            "sensitive_material_compared": False,
        },
        capability=CAP_DIAGNOSTICS_READ,
        target_type="organization_comparison",
        target_id=str(target.id),
        audit_summary={
            "diagnostic": "configuration_drift",
            "same_configuration": same,
            "changed_section_count": sum(1 for row in rows if not row["same"]),
            "target_organization_id": str(target.id),
        },
    )


def get_configuration_integrity_diagnostics(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_DIAGNOSTICS_READ,
    )

    duplicates = []
    pipelines = list(
        Pipeline.objects.filter(organization=organization).only(
            "id", "name", "is_active"
        )
    )
    seen = {}
    for item in pipelines:
        key = item.name.strip().casefold()
        seen.setdefault(key, []).append(item)
    for values in seen.values():
        if len(values) > 1:
            duplicates.append(
                {
                    "type": "pipeline_name",
                    "count": len(values),
                    "active_count": sum(1 for item in values if item.is_active),
                }
            )

    stages = list(
        Stage.objects.filter(pipeline__organization=organization)
        .select_related("pipeline")
        .only("id", "name", "is_active", "pipeline_id", "pipeline__is_active")
    )
    stage_seen = {}
    for item in stages:
        key = (str(item.pipeline_id), item.name.strip().casefold())
        stage_seen.setdefault(key, []).append(item)
    for values in stage_seen.values():
        if len(values) > 1:
            duplicates.append(
                {
                    "type": "stage_name",
                    "count": len(values),
                    "active_count": sum(1 for item in values if item.is_active),
                }
            )

    workflow_names = {}
    workflows = list(
        SmartTrigger.objects.filter(organization=organization).only(
            "id",
            "name",
            "enabled",
            "is_active",
            "trigger_type",
            "conditions",
            "action_type",
            "action",
        )
    )
    for item in workflows:
        workflow_names.setdefault(item.name.strip().casefold(), []).append(item)
    for values in workflow_names.values():
        if len(values) > 1:
            duplicates.append(
                {
                    "type": "workflow_name",
                    "count": len(values),
                    "active_count": sum(1 for item in values if item.is_active),
                }
            )

    orphans = []
    for stage in stages:
        if stage.is_active and not stage.pipeline.is_active:
            orphans.append(
                {
                    "type": "active_stage_in_inactive_pipeline",
                    "object_id": str(stage.id),
                }
            )

    cadences = list(
        FollowupSequence.objects.filter(organization=organization)
        .select_related("whatsapp_account")
        .defer("whatsapp_account__access_token")
    )
    for cadence in cadences:
        if cadence.is_active and (
            not cadence.whatsapp_account.is_active
            or cadence.whatsapp_account.status != WhatsAppAccount.Status.CONNECTED
        ):
            orphans.append(
                {
                    "type": "active_cadence_with_unavailable_sender",
                    "object_id": str(cadence.id),
                }
            )

    for workflow in workflows:
        if not workflow.is_active:
            continue
        payload = {
            "name": workflow.name,
            "enabled": workflow.enabled,
            "trigger_type": workflow.trigger_type,
            "conditions": workflow.conditions,
            "action_type": workflow.action_type,
            "action": workflow.action,
        }
        try:
            validate_workflow_rule(organization, payload)
        except ValidationError:
            orphans.append(
                {
                    "type": "workflow_with_invalid_or_missing_reference",
                    "object_id": str(workflow.id),
                }
            )

    attributes = list(
        AttributeDefinition.objects.filter(organization=organization).only(
            "id", "key", "name", "is_active"
        )
    )
    active_keys = {item.key for item in attributes if item.is_active}
    inactive_keys = {item.key for item in attributes if not item.is_active}
    archived_workflow_refs = []
    for workflow in workflows:
        if not workflow.is_active:
            continue
        conditions = workflow.conditions if isinstance(workflow.conditions, dict) else {}
        action = workflow.action if isinstance(workflow.action, dict) else {}
        refs = {
            str(item.get("key") or "")
            for item in conditions.get("attributes") or []
            if isinstance(item, dict)
        }
        refs.add(str(action.get("key") or ""))
        refs.add(str(action.get("date_attribute") or ""))
        refs.discard("")
        stale = sorted((refs - active_keys) | (refs & inactive_keys))
        if stale:
            archived_workflow_refs.append(
                {
                    "workflow_id": str(workflow.id),
                    "reference_count": len(stale),
                }
            )
    orphans.extend(
        {
            "type": "workflow_references_inactive_or_missing_attribute",
            "object_id": item["workflow_id"],
            "reference_count": item["reference_count"],
        }
        for item in archived_workflow_refs
    )

    return ToolExecution(
        data={
            "valid": not duplicates and not orphans,
            "duplicate_groups": duplicates,
            "orphan_references": orphans,
            "counts": {
                "duplicate_groups": len(duplicates),
                "orphan_references": len(orphans),
            },
        },
        capability=CAP_DIAGNOSTICS_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={
            "diagnostic": "configuration_integrity",
            "duplicate_groups": len(duplicates),
            "orphan_references": len(orphans),
        },
    )


def test_ai_response_policy(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_DIAGNOSTICS_READ,
    )
    info = OrgInfo.objects.filter(organization=organization).first()
    raw = str(getattr(info, "ai_playbook", "") or "")
    policy = compile_engagement_instruction_policy(raw)

    from apps.integrations.operations_extended_tools import (
        _qualification_public_snapshot,
        simulate_ai_conversation,
    )

    qualification = _qualification_public_snapshot(organization)
    issues = []
    if not raw.strip():
        issues.append(
            {
                "severity": "error",
                "code": "missing_ai_playbook",
            }
        )
    if qualification.get("errors"):
        issues.append(
            {
                "severity": "error",
                "code": "qualification_configuration_errors",
                "count": len(qualification["errors"]),
            }
        )
    if qualification.get("requirements") and not policy.get("qualification_criteria"):
        issues.append(
            {
                "severity": "warning",
                "code": "missing_qualification_criteria",
            }
        )
    if qualification.get("requirements") and not qualification.get("completion_stage"):
        issues.append(
            {
                "severity": "error",
                "code": "missing_completion_stage",
            }
        )
    if qualification.get("requirements") and not qualification.get("final_ack"):
        issues.append(
            {
                "severity": "warning",
                "code": "missing_final_acknowledgement",
            }
        )

    answers = (arguments or {}).get("answers")
    simulation = None
    if answers is not None:
        execution = simulate_ai_conversation(
            identity=identity,
            arguments={"answers": answers},
        )
        simulation = execution.data

    return ToolExecution(
        data={
            "valid": not any(item["severity"] == "error" for item in issues),
            "policy": {
                "qualification_completion": policy.get("qualification_completion"),
                "qualification_criteria_count": len(
                    policy.get("qualification_criteria") or []
                ),
                "stage_shifting_rule_count": len(policy.get("stage_shifting") or []),
                "attribute_mapping_rule_count": len(
                    policy.get("attribute_mapped") or []
                ),
                "reminder_rule_count": len(policy.get("reminders") or []),
            },
            "qualification": {
                "configured": qualification.get("configured"),
                "flow_version": qualification.get("flow_version"),
                "requirement_count": len(qualification.get("requirements") or []),
                "completion_stage_configured": bool(
                    qualification.get("completion_stage")
                ),
                "mapping_count": sum(
                    len(value)
                    for value in (qualification.get("mappings") or {}).values()
                ),
            },
            "issues": issues,
            "simulation": simulation,
            "side_effects": False,
            "messages_sent": 0,
            "leads_created": 0,
        },
        capability=CAP_DIAGNOSTICS_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={
            "diagnostic": "ai_response_policy",
            "valid": not any(item["severity"] == "error" for item in issues),
            "issue_count": len(issues),
            "simulation": answers is not None,
        },
    )


SUPERADMIN_DIAGNOSTIC_HANDLERS = {
    "test_integration_connection": test_integration_connection,
    "compare_organization_configuration": compare_organization_configuration,
    "get_configuration_integrity_diagnostics": get_configuration_integrity_diagnostics,
    "test_ai_response_policy": test_ai_response_policy,
}
