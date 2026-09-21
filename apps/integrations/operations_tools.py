from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.ai_engagement.models import OrgInfo
from apps.ai_engagement.services.confidentiality import is_sensitive_attribute_definition
from apps.ai_engagement.services.organization_profile import compile_qualification_requirements
from apps.ai_engagement.services.playbook import qualification_questions
from apps.ai_engagement.services.qualification_state import (
    QUALIFIED_STAGE,
    normalize_stage_name,
    requirements_for_lead,
    state_for_lead,
)
from apps.crm.models import AttributeDefinition, Lead, LeadActivity, Pipeline, Stage
from apps.followups.models import FollowupSequence
from apps.integrations.diagnostic_tools import (
    DiagnosticToolError,
    execute_tool as execute_diagnostic_tool,
)
from apps.integrations.operations_models import (
    OperationsAuditEvent,
    OperationsSupportSession,
)
from apps.integrations.operations_policy import (
    CAP_AI_CONFIG_WRITE,
    CAP_AUDIT_READ,
    CAP_DIAGNOSTICS_READ,
    CAP_LEAD_ATTRIBUTES_WRITE,
    CAP_LEAD_STAGE_WRITE,
    CAP_ORGANIZATION_READ,
    ROLE_ORGANIZATION_ADMIN,
    ROLE_SUPERADMIN,
    approval_required,
    effective_capabilities,
    policy_for,
    require_capability,
    OperationsPolicyError,
)
from apps.organizations.models import Organization
from apps.triggers.models import SmartTrigger, TriggerRun
from services.crm.attribute_service import update_lead_attribute_values
from services.crm.lead_transition import (
    LeadTransitionError,
    move_lead_to_pipeline_stage,
)


DIAGNOSTIC_TOOL_NAMES = {
    "find_leads",
    "get_lead_snapshot",
    "get_conversation",
    "trace_message",
    "get_ai_diagnostics",
    "get_integration_health",
    "get_workflow_trace",
    "get_recent_errors",
    "get_runtime_health",
}


class OperationsToolError(ValueError):
    code = "operations_input_error"
    outcome = OperationsAuditEvent.Outcome.ERROR


class OperationsPermissionError(OperationsToolError):
    code = "operations_permission_denied"
    outcome = OperationsAuditEvent.Outcome.DENIED


class OperationsApprovalRequired(OperationsToolError):
    code = "operations_approval_required"
    outcome = OperationsAuditEvent.Outcome.APPROVAL_REQUIRED


@dataclass
class ToolExecution:
    data: dict
    capability: str = ""
    target_type: str = ""
    target_id: str = ""
    reason: str = ""
    outcome: str = OperationsAuditEvent.Outcome.SUCCESS
    audit_summary: dict | None = None


def _uuid(value, *, field):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise OperationsToolError(f"{field} must be a valid UUID.") from exc


def _organization_for(identity, *, required=True):
    organization = (
        identity.active_organization
        if identity.role == ROLE_SUPERADMIN
        else identity.organization
    )
    if required and organization is None:
        raise OperationsToolError(
            "Select an organization support context before using this tool."
        )
    return organization


def _reason(arguments, *, required=False):
    value = str((arguments or {}).get("reason") or "").strip()
    if required and len(value) < 8:
        raise OperationsToolError(
            "A specific reason of at least 8 characters is required for this mutation."
        )
    return value[:500]


def _write_gate(*, identity, organization, capability, arguments):
    try:
        require_capability(
            role=identity.role,
            organization=organization,
            capability=capability,
        )
    except OperationsPolicyError as exc:
        raise OperationsPermissionError(str(exc)) from exc

    if "operations.write" not in identity.scopes:
        raise OperationsPermissionError(
            "This OAuth token does not include operations.write."
        )

    dry_run = bool((arguments or {}).get("dry_run", True))
    approved = bool((arguments or {}).get("approved", False))
    reason = _reason(arguments, required=True)
    if (
        not dry_run
        and approval_required(
            role=identity.role,
            organization=organization,
            capability=capability,
        )
        and not approved
    ):
        raise OperationsApprovalRequired(
            "Approval is required. Re-run with approved=true only after the human has approved the displayed dry-run."
        )
    return dry_run, reason


def _lead(organization, lead_id):
    lead = (
        Lead.objects.select_related("organization", "pipeline", "stage")
        .filter(pk=_uuid(lead_id, field="lead_id"), organization=organization)
        .first()
    )
    if lead is None:
        raise OperationsToolError("Lead not found in the active organization.")
    return lead


def _support_session(identity):
    if identity.role != ROLE_SUPERADMIN:
        return None
    return (
        OperationsSupportSession.objects.filter(
            token=identity.token,
            ended_at__isnull=True,
        )
        .select_related("organization", "actor")
        .order_by("-started_at")
        .first()
    )


def get_operations_context(*, identity, arguments):
    organization = _organization_for(identity, required=False)
    capabilities = sorted(
        effective_capabilities(
            role=identity.role,
            organization=organization,
        )
    )
    session = _support_session(identity)
    policy = policy_for(organization) if organization is not None else None
    return ToolExecution(
        data={
            "role": identity.role,
            "actor": {
                "id": str(identity.actor.id),
                "name": identity.actor.name,
            },
            "organization": (
                {
                    "id": str(organization.id),
                    "name": organization.name,
                    "active": organization.is_active,
                }
                if organization is not None
                else None
            ),
            "capabilities": capabilities,
            "organization_admin_external_ai_enabled": (
                policy.organization_admin_enabled if policy else None
            ),
            "superadmin_support_session": (
                {
                    "id": str(session.id),
                    "started_at": session.started_at.isoformat(),
                    "reason": session.reason,
                }
                if session is not None
                else None
            ),
        },
        capability=CAP_ORGANIZATION_READ,
        target_type="organization" if organization else "platform",
        target_id=str(organization.id) if organization else "",
        audit_summary={"context_present": organization is not None},
    )


def list_organizations(*, identity, arguments):
    if identity.role != ROLE_SUPERADMIN:
        raise OperationsPermissionError("Only SHVYA Superadmin can list organizations.")
    query = str((arguments or {}).get("query") or "").strip()
    try:
        limit = int((arguments or {}).get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 50))

    qs = Organization.objects.all().order_by("name")
    if query:
        qs = qs.filter(Q(name__icontains=query) | Q(users__email__icontains=query)).distinct()
    rows = []
    for organization in qs[:limit]:
        policy = policy_for(organization)
        active_support = OperationsSupportSession.objects.filter(
            organization=organization,
            ended_at__isnull=True,
        ).exists()
        rows.append(
            {
                "id": str(organization.id),
                "name": organization.name,
                "active": organization.is_active,
                "organization_admin_external_ai_enabled": policy.organization_admin_enabled,
                "superadmin_support_active": active_support,
            }
        )
    return ToolExecution(
        data={"organizations": rows, "count": len(rows)},
        capability=CAP_ORGANIZATION_READ,
        target_type="platform",
        audit_summary={"result_count": len(rows)},
    )


def select_organization_context(*, identity, arguments):
    if identity.role != ROLE_SUPERADMIN:
        raise OperationsPermissionError(
            "Organization context selection is available only to SHVYA Superadmin."
        )
    if "operations.write" not in identity.scopes:
        raise OperationsPermissionError(
            "This OAuth token does not include operations.write."
        )
    reason = _reason(arguments, required=True)
    organization = Organization.objects.filter(
        pk=_uuid((arguments or {}).get("organization_id"), field="organization_id"),
        is_active=True,
    ).first()
    if organization is None:
        raise OperationsToolError("Active organization not found.")

    now = timezone.now()
    with transaction.atomic():
        token = identity.token.__class__.objects.select_for_update().get(pk=identity.token.pk)
        OperationsSupportSession.objects.filter(
            token=token,
            ended_at__isnull=True,
        ).update(ended_at=now, last_seen_at=now)
        token.active_organization = organization
        token.save(update_fields=["active_organization", "updated_at"])
        session = OperationsSupportSession.objects.create(
            token=token,
            actor=identity.actor,
            organization=organization,
            reason=reason,
        )
    identity.token.active_organization = organization
    return ToolExecution(
        data={
            "status": "selected",
            "organization": {"id": str(organization.id), "name": organization.name},
            "support_session_id": str(session.id),
            "organization_visibility": "SHVYA Support / Superadmin access is visible to organization admins while this context is active.",
        },
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        reason=reason,
        audit_summary={"support_context": "started"},
    )


def clear_organization_context(*, identity, arguments):
    if identity.role != ROLE_SUPERADMIN:
        raise OperationsPermissionError(
            "Organization context clearing is available only to SHVYA Superadmin."
        )
    if "operations.write" not in identity.scopes:
        raise OperationsPermissionError(
            "This OAuth token does not include operations.write."
        )
    reason = _reason(arguments, required=True)
    previous = identity.token.active_organization
    now = timezone.now()
    with transaction.atomic():
        token = identity.token.__class__.objects.select_for_update().get(pk=identity.token.pk)
        OperationsSupportSession.objects.filter(
            token=token,
            ended_at__isnull=True,
        ).update(ended_at=now, last_seen_at=now)
        token.active_organization = None
        token.save(update_fields=["active_organization", "updated_at"])
    return ToolExecution(
        data={"status": "cleared"},
        capability=CAP_ORGANIZATION_READ,
        target_type="organization" if previous else "platform",
        target_id=str(previous.id) if previous else "",
        reason=reason,
        audit_summary={"support_context": "ended"},
    )


def get_organization_configuration(*, identity, arguments):
    organization = _organization_for(identity)
    try:
        require_capability(
            role=identity.role,
            organization=organization,
            capability=CAP_ORGANIZATION_READ,
        )
    except OperationsPolicyError as exc:
        raise OperationsPermissionError(str(exc)) from exc

    info = OrgInfo.objects.filter(organization=organization).first()
    pipelines = list(
        Pipeline.objects.filter(organization=organization)
        .prefetch_related("stages")
        .order_by("name")
    )
    attributes = list(
        AttributeDefinition.objects.filter(organization=organization)
        .order_by("display_order", "name")
    )
    playbook = str(getattr(info, "ai_playbook", "") or "")
    compiled = compile_qualification_requirements(qualification_questions(playbook))
    return ToolExecution(
        data={
            "organization": {
                "id": str(organization.id),
                "name": organization.name,
                "active": organization.is_active,
            },
            "ai": {
                "configured": info is not None,
                "about": str(getattr(info, "about", "") or "")[:4000],
                "bot_languages": str(getattr(info, "bot_languages", "") or "")[:500],
                "ai_enabled": bool(getattr(info, "ai_enabled", True)) if info else None,
                "bump_up_enabled": bool(getattr(info, "bump_up_enabled", True)) if info else None,
                "bump_up_count": int(getattr(info, "bump_up_count", 0)) if info else None,
                "playbook": playbook[:30000],
                "qualification": {
                    "mode": compiled.get("mode"),
                    "flow_version": compiled.get("flow_version"),
                    "requirements": compiled.get("requirements", []),
                },
            },
            "pipelines": [
                {
                    "id": str(pipeline.id),
                    "name": pipeline.name,
                    "active": pipeline.is_active,
                    "ai_enabled": pipeline.ai_enabled,
                    "stages": [
                        {
                            "id": str(stage.id),
                            "name": stage.name,
                            "active": stage.is_active,
                            "ai_on": stage.ai_on,
                            "display_order": stage.display_order,
                            "description": stage.description[:1000],
                        }
                        for stage in pipeline.stages.all()
                    ],
                }
                for pipeline in pipelines
            ],
            "attributes": [
                {
                    "id": str(item.id),
                    "key": item.key,
                    "name": item.name,
                    "field_type": item.field_type,
                    "description": item.description[:500],
                    "options": item.options,
                }
                for item in attributes
            ],
            "automation": {
                "workflow_count": SmartTrigger.objects.filter(organization=organization).count(),
                "workflow_enabled_count": SmartTrigger.objects.filter(
                    organization=organization, enabled=True
                ).count(),
                "cadence_count": FollowupSequence.objects.filter(organization=organization).count(),
                "cadence_active_count": FollowupSequence.objects.filter(
                    organization=organization, is_active=True
                ).count(),
            },
        },
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={
            "pipelines": len(pipelines),
            "attributes": len(attributes),
            "qualification_requirements": len(compiled.get("requirements", [])),
        },
    )


def _qualification_snapshot(lead):
    info = OrgInfo.objects.filter(organization=lead.organization).first()
    raw = str(getattr(info, "ai_playbook", "") or "")
    compiled = compile_qualification_requirements(qualification_questions(raw))
    requirements = requirements_for_lead(lead, compiled.get("requirements", []))
    state = state_for_lead(lead, requirements=requirements)
    return compiled, requirements, state


def diagnose_lead_qualification(*, identity, arguments):
    organization = _organization_for(identity)
    try:
        require_capability(
            role=identity.role,
            organization=organization,
            capability=CAP_DIAGNOSTICS_READ,
        )
    except OperationsPolicyError as exc:
        raise OperationsPermissionError(str(exc)) from exc
    lead = _lead(organization, (arguments or {}).get("lead_id"))
    compiled, requirements, state = _qualification_snapshot(lead)

    current_is_qualified = normalize_stage_name(lead.stage.name) == QUALIFIED_STAGE
    target_id = state.get("qualified_stage_id")
    target = (
        Stage.objects.filter(pk=target_id, pipeline=lead.pipeline, is_active=True).first()
        if target_id
        else None
    )

    if current_is_qualified:
        classification = "NO_PROBLEM_FOUND"
        root_cause = "Lead is already in the Qualified stage."
        repair_available = False
    elif state.get("qualification_status") == "completed" and target is None:
        classification = "ROOT_CAUSE_CONFIRMED"
        root_cause = "Qualification is completed, but no active Qualified stage exists in the lead's current pipeline."
        repair_available = False
    elif (
        state.get("qualification_status") == "completed"
        and target is not None
        and not current_is_qualified
    ):
        classification = "ROOT_CAUSE_CONFIRMED"
        root_cause = "Qualification is completed and an active Qualified target exists, but the lead is not in that target stage."
        repair_available = True
    elif state.get("all_requirements_answered") and target is not None:
        classification = "LIKELY_CAUSE"
        root_cause = "All configured requirements are answered but qualification completion/stage reconciliation has not completed."
        repair_available = False
    else:
        classification = "INSUFFICIENT_EVIDENCE"
        root_cause = "Qualification is not yet complete, or the persisted evidence does not establish a failed Qualified transition."
        repair_available = False

    latest_inbound = (
        lead.whatsapp_messages.filter(
            organization=organization,
            direction="inbound",
        )
        .order_by("-created_at", "-id")
        .first()
    )
    processing = {}
    if latest_inbound is not None and isinstance(latest_inbound.raw_payload, dict):
        raw_processing = latest_inbound.raw_payload.get("shvya_ai_processing")
        if isinstance(raw_processing, dict):
            processing = {
                key: raw_processing.get(key)
                for key in (
                    "processed",
                    "qualification_execution_status",
                    "qualification_reconciliation_status",
                )
                if key in raw_processing
            }

    return ToolExecution(
        data={
            "classification": classification,
            "root_cause": root_cause,
            "repair_available": repair_available,
            "lead": {
                "id": str(lead.id),
                "pipeline_id": str(lead.pipeline_id),
                "pipeline": lead.pipeline.name,
                "stage_id": str(lead.stage_id),
                "stage": lead.stage.name,
            },
            "qualification": {
                "mode": compiled.get("mode"),
                "flow_version": state.get("flow_version") or compiled.get("flow_version"),
                "status": state.get("qualification_status"),
                "result": state.get("qualification_result"),
                "all_requirements_answered": state.get("all_requirements_answered"),
                "answered_requirement_ids": state.get("answered_requirement_ids"),
                "missing_requirement_ids": state.get("missing_requirement_ids"),
                "target_stage_id": state.get("qualified_stage_id"),
                "target_stage_name": state.get("qualified_stage_name"),
                "requirements": [
                    {
                        "id": item.get("id"),
                        "stable_id": item.get("stable_id"),
                        "question": item.get("question"),
                        "required": item.get("required"),
                    }
                    for item in requirements
                ],
            },
            "latest_processing_markers": processing,
        },
        capability=CAP_DIAGNOSTICS_READ,
        target_type="lead",
        target_id=str(lead.id),
        audit_summary={
            "classification": classification,
            "repair_available": repair_available,
        },
    )


def move_lead_stage(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_LEAD_STAGE_WRITE,
        arguments=arguments,
    )
    lead = _lead(organization, (arguments or {}).get("lead_id"))
    stage = (
        Stage.objects.select_related("pipeline")
        .filter(
            pk=_uuid((arguments or {}).get("target_stage_id"), field="target_stage_id"),
            pipeline__organization=organization,
            pipeline__is_active=True,
            is_active=True,
        )
        .first()
    )
    if stage is None:
        raise OperationsToolError("Active target stage was not found in this organization.")

    before = {
        "pipeline_id": str(lead.pipeline_id),
        "pipeline": lead.pipeline.name,
        "stage_id": str(lead.stage_id),
        "stage": lead.stage.name,
    }
    after = {
        "pipeline_id": str(stage.pipeline_id),
        "pipeline": stage.pipeline.name,
        "stage_id": str(stage.id),
        "stage": stage.name,
    }
    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "proposed_change": {"before": before, "after": after},
                "risk": "Moves one lead and records canonical CRM pipeline/stage activity.",
                "reversible": True,
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_LEAD_STAGE_WRITE,
                ),
            },
            capability=CAP_LEAD_STAGE_WRITE,
            target_type="lead",
            target_id=str(lead.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={
                "operation": "move_lead_stage",
                "from_stage_id": str(lead.stage_id),
                "to_stage_id": str(stage.id),
            },
        )

    try:
        move_lead_to_pipeline_stage(
            lead=lead,
            pipeline=stage.pipeline,
            stage=stage,
            actor=identity.actor,
        )
    except LeadTransitionError as exc:
        raise OperationsToolError(str(exc)) from exc
    lead.refresh_from_db(fields=["pipeline", "stage", "stage_entered_at", "updated_at"])
    if lead.stage_id != stage.id or lead.pipeline_id != stage.pipeline_id:
        raise OperationsToolError("Stage repair write completed but verification did not match the requested state.")

    return ToolExecution(
        data={
            "status": "FIXED",
            "before": before,
            "after": after,
            "verification": "passed",
        },
        capability=CAP_LEAD_STAGE_WRITE,
        target_type="lead",
        target_id=str(lead.id),
        reason=reason,
        audit_summary={
            "operation": "move_lead_stage",
            "from_stage_id": before["stage_id"],
            "to_stage_id": after["stage_id"],
            "verification": "passed",
        },
    )


def repair_qualification_stage(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_LEAD_STAGE_WRITE,
        arguments=arguments,
    )
    lead = _lead(organization, (arguments or {}).get("lead_id"))
    _, _, state = _qualification_snapshot(lead)
    if state.get("qualification_status") != "completed":
        raise OperationsToolError(
            "Qualification is not completed; SHVYA will not force a Qualified stage transition."
        )
    target_id = state.get("qualified_stage_id")
    target = (
        Stage.objects.select_related("pipeline")
        .filter(
            pk=target_id,
            pipeline=lead.pipeline,
            pipeline__organization=organization,
            pipeline__is_active=True,
            is_active=True,
        )
        .first()
        if target_id
        else None
    )
    if target is None:
        raise OperationsToolError(
            "No active Qualified target exists in the lead's current pipeline."
        )
    nested = dict(arguments or {})
    nested["target_stage_id"] = str(target.id)
    nested["dry_run"] = dry_run
    nested["reason"] = reason
    return move_lead_stage(identity=identity, arguments=nested)


def update_lead_attributes(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_LEAD_ATTRIBUTES_WRITE,
        arguments=arguments,
    )
    lead = _lead(organization, (arguments or {}).get("lead_id"))
    values = (arguments or {}).get("values")
    if not isinstance(values, dict) or not values:
        raise OperationsToolError("values must be a non-empty object keyed by CRM attribute key.")

    definitions = {
        item.key: item
        for item in AttributeDefinition.objects.filter(organization=organization)
    }
    unknown = sorted(set(values) - set(definitions))
    if unknown:
        raise OperationsToolError(
            "Unknown organization attribute keys: " + ", ".join(unknown[:10])
        )
    sensitive = [
        key
        for key in values
        if is_sensitive_attribute_definition(
            {"key": definitions[key].key, "name": definitions[key].name}
        )
    ]
    if sensitive:
        raise OperationsPermissionError(
            "Sensitive/credential-like attributes cannot be written through Operations MCP."
        )

    before = {
        key: (lead.attributes or {}).get(key)
        for key in values
    }
    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "lead_id": str(lead.id),
                "attribute_keys": sorted(values),
                "changed_keys": sorted(
                    key
                    for key, value in values.items()
                    if str(before.get(key) or "") != str(value or "").strip()
                ),
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_LEAD_ATTRIBUTES_WRITE,
                ),
                "risk": "Updates only existing non-sensitive organization-defined attributes for this lead.",
                "reversible": True,
            },
            capability=CAP_LEAD_ATTRIBUTES_WRITE,
            target_type="lead",
            target_id=str(lead.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={"attribute_keys": sorted(values), "operation": "update_lead_attributes"},
        )

    try:
        update_lead_attribute_values(
            organization=organization,
            lead=lead,
            values=values,
        )
    except ValidationError as exc:
        raise OperationsToolError("Lead attribute validation failed.") from exc
    lead.refresh_from_db(fields=["attributes", "updated_at"])
    failed = [
        key
        for key, value in values.items()
        if str((lead.attributes or {}).get(key) or "") != str(value or "").strip()
    ]
    if failed:
        raise OperationsToolError(
            "Attribute write verification failed for: " + ", ".join(failed[:10])
        )
    return ToolExecution(
        data={
            "status": "FIXED",
            "lead_id": str(lead.id),
            "updated_keys": sorted(values),
            "verification": "passed",
        },
        capability=CAP_LEAD_ATTRIBUTES_WRITE,
        target_type="lead",
        target_id=str(lead.id),
        reason=reason,
        audit_summary={
            "attribute_keys": sorted(values),
            "operation": "update_lead_attributes",
            "verification": "passed",
        },
    )


def update_ai_configuration(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_AI_CONFIG_WRITE,
        arguments=arguments,
    )
    changes = (arguments or {}).get("changes")
    if not isinstance(changes, dict) or not changes:
        raise OperationsToolError("changes must be a non-empty object.")

    allowed = {
        "about",
        "bot_languages",
        "ai_playbook",
        "ai_enabled",
        "bump_up_enabled",
        "bump_up_count",
    }
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise OperationsToolError(
            "Unsupported AI configuration fields: " + ", ".join(unknown)
        )

    normalized = {}
    for key, value in changes.items():
        if key in {"about", "bot_languages", "ai_playbook"}:
            text = str(value or "").strip()
            limits = {"about": 12000, "bot_languages": 500, "ai_playbook": 50000}
            if len(text) > limits[key]:
                raise OperationsToolError(f"{key} is too large.")
            normalized[key] = text
        elif key in {"ai_enabled", "bump_up_enabled"}:
            if not isinstance(value, bool):
                raise OperationsToolError(f"{key} must be true or false.")
            normalized[key] = value
        elif key == "bump_up_count":
            try:
                count = int(value)
            except (TypeError, ValueError) as exc:
                raise OperationsToolError("bump_up_count must be an integer.") from exc
            if count < 0 or count > 20:
                raise OperationsToolError("bump_up_count must be between 0 and 20.")
            normalized[key] = count

    playbook_validation = None
    if "ai_playbook" in normalized:
        compiled = compile_qualification_requirements(
            qualification_questions(normalized["ai_playbook"])
        )
        playbook_validation = {
            "qualification_mode": compiled.get("mode"),
            "flow_version": compiled.get("flow_version"),
            "requirement_count": len(compiled.get("requirements", [])),
        }

    info, _ = OrgInfo.objects.get_or_create(organization=organization)
    changed_fields = [
        key
        for key, value in normalized.items()
        if getattr(info, key) != value
    ]

    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "changed_fields": changed_fields,
                "playbook_validation": playbook_validation,
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_AI_CONFIG_WRITE,
                ),
                "risk": "Changes organization-level customer-facing AI behavior. Existing backend safety, tenant, qualification, and CRM execution rules remain authoritative.",
                "reversible": True,
            },
            capability=CAP_AI_CONFIG_WRITE,
            target_type="organization",
            target_id=str(organization.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={"changed_fields": changed_fields, "operation": "update_ai_configuration"},
        )

    for key, value in normalized.items():
        setattr(info, key, value)
    if normalized:
        info.save(update_fields=[*normalized.keys(), "updated_at"])
    info.refresh_from_db()
    verification_failed = [
        key for key, value in normalized.items() if getattr(info, key) != value
    ]
    if verification_failed:
        raise OperationsToolError(
            "AI configuration verification failed for: "
            + ", ".join(verification_failed)
        )
    return ToolExecution(
        data={
            "status": "FIXED",
            "changed_fields": changed_fields,
            "playbook_validation": playbook_validation,
            "verification": "passed",
        },
        capability=CAP_AI_CONFIG_WRITE,
        target_type="organization",
        target_id=str(organization.id),
        reason=reason,
        audit_summary={
            "changed_fields": changed_fields,
            "operation": "update_ai_configuration",
            "verification": "passed",
        },
    )


def get_conversion_analysis(*, identity, arguments):
    organization = _organization_for(identity)
    try:
        require_capability(
            role=identity.role,
            organization=organization,
            capability=CAP_DIAGNOSTICS_READ,
        )
    except OperationsPolicyError as exc:
        raise OperationsPermissionError(str(exc)) from exc
    try:
        days = int((arguments or {}).get("days") or 30)
    except (TypeError, ValueError):
        days = 30
    days = max(7, min(days, 90))
    now = timezone.now()
    current_start = now - timedelta(days=days)
    previous_start = current_start - timedelta(days=days)

    def window(start, end):
        created = Lead.objects.filter(
            organization=organization,
            created_at__gte=start,
            created_at__lt=end,
        )
        qualified_ids = (
            LeadActivity.objects.filter(
                organization=organization,
                topic__in=[
                    LeadActivity.Topic.STAGE_CHANGED,
                    LeadActivity.Topic.PIPELINE_CHANGED,
                ],
                created_at__gte=start,
                created_at__lt=end,
                new_stage_name__iexact="Qualified",
            )
            .values_list("lead_id", flat=True)
            .distinct()
        )
        lead_count = created.count()
        qualified_count = qualified_ids.count()
        return {
            "lead_volume": lead_count,
            "qualified_transitions": qualified_count,
            "qualified_transition_rate": (
                round(qualified_count / lead_count, 4) if lead_count else None
            ),
        }

    current = window(current_start, now)
    previous = window(previous_start, current_start)
    failures_current = TriggerRun.objects.filter(
        rule__organization=organization,
        status__in=["failed", "error"],
        created_at__gte=current_start,
        created_at__lt=now,
    ).count()
    failures_previous = TriggerRun.objects.filter(
        rule__organization=organization,
        status__in=["failed", "error"],
        created_at__gte=previous_start,
        created_at__lt=current_start,
    ).count()

    observations = []
    if current["lead_volume"] != previous["lead_volume"]:
        observations.append(
            {
                "classification": "Measured",
                "metric": "lead_volume",
                "current": current["lead_volume"],
                "previous": previous["lead_volume"],
            }
        )
    if current["qualified_transition_rate"] != previous["qualified_transition_rate"]:
        observations.append(
            {
                "classification": "Measured",
                "metric": "qualified_transition_rate",
                "current": current["qualified_transition_rate"],
                "previous": previous["qualified_transition_rate"],
            }
        )
    if failures_current > failures_previous:
        observations.append(
            {
                "classification": "Likely contributor",
                "metric": "workflow_failures",
                "current": failures_current,
                "previous": failures_previous,
                "note": "Workflow failures increased during the comparison window; this is correlation, not proof of conversion causality.",
            }
        )

    return ToolExecution(
        data={
            "comparison": {
                "days_per_period": days,
                "current_period": {
                    "start": current_start.isoformat(),
                    "end": now.isoformat(),
                    **current,
                    "workflow_failures": failures_current,
                },
                "previous_period": {
                    "start": previous_start.isoformat(),
                    "end": current_start.isoformat(),
                    **previous,
                    "workflow_failures": failures_previous,
                },
            },
            "observations": observations,
            "limitations": [
                "Qualified transition rate uses persisted CRM stage activity and leads created in each period.",
                "This tool does not claim causality from correlation alone.",
            ],
        },
        capability=CAP_DIAGNOSTICS_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={"period_days": days, "observation_count": len(observations)},
    )


def get_operations_audit(*, identity, arguments):
    organization = _organization_for(identity)
    try:
        require_capability(
            role=identity.role,
            organization=organization,
            capability=CAP_AUDIT_READ,
        )
    except OperationsPolicyError as exc:
        raise OperationsPermissionError(str(exc)) from exc
    try:
        limit = int((arguments or {}).get("limit") or 30)
    except (TypeError, ValueError):
        limit = 30
    limit = max(1, min(limit, 100))
    rows = OperationsAuditEvent.objects.filter(organization=organization).select_related("actor")
    if identity.role != ROLE_SUPERADMIN:
        # Organization admins can see Operations AI activity in their tenant,
        # including visible SHVYA Support actions, but never another tenant.
        rows = rows.filter(organization=organization)
    rows = rows.order_by("-created_at")[:limit]
    return ToolExecution(
        data={
            "events": [
                {
                    "id": str(item.id),
                    "actor": item.actor.name if item.actor else "Former user",
                    "role": item.role,
                    "tool": item.tool_name,
                    "capability": item.capability,
                    "target_type": item.target_type,
                    "target_id": item.target_id,
                    "reason": item.reason,
                    "outcome": item.outcome,
                    "change_summary": item.change_summary,
                    "error_code": item.error_code,
                    "created_at": item.created_at.isoformat(),
                }
                for item in rows
            ]
        },
        capability=CAP_AUDIT_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={"result_count": len(rows)},
    )


def execute_operations_tool(*, name, identity, arguments):
    if name in DIAGNOSTIC_TOOL_NAMES:
        organization = _organization_for(identity)
        try:
            require_capability(
                role=identity.role,
                organization=organization,
                capability=CAP_DIAGNOSTICS_READ,
            )
        except OperationsPolicyError as exc:
            raise OperationsPermissionError(str(exc)) from exc
        try:
            data = execute_diagnostic_tool(
                name=name,
                organization=organization,
                arguments=arguments or {},
            )
        except DiagnosticToolError as exc:
            raise OperationsToolError(str(exc)) from exc
        target_id = str((arguments or {}).get("lead_id") or (arguments or {}).get("message_id") or "")
        return ToolExecution(
            data=data,
            capability=CAP_DIAGNOSTICS_READ,
            target_type="lead_or_message" if target_id else "organization",
            target_id=target_id or str(organization.id),
            audit_summary={"diagnostic_tool": name},
        )

    handlers = {
        "get_operations_context": get_operations_context,
        "list_organizations": list_organizations,
        "select_organization_context": select_organization_context,
        "clear_organization_context": clear_organization_context,
        "get_organization_configuration": get_organization_configuration,
        "diagnose_lead_qualification": diagnose_lead_qualification,
        "move_lead_stage": move_lead_stage,
        "repair_qualification_stage": repair_qualification_stage,
        "update_lead_attributes": update_lead_attributes,
        "update_ai_configuration": update_ai_configuration,
        "get_conversion_analysis": get_conversion_analysis,
        "get_operations_audit": get_operations_audit,
    }
    handler = handlers.get(str(name or ""))
    if handler is None:
        raise OperationsToolError("Unknown SHVYA Operations tool.")
    return handler(identity=identity, arguments=arguments or {})
