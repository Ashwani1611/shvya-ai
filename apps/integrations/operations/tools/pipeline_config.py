# ruff: noqa: F401
"""Focused Operations MCP configuration tools: pipeline config."""

from __future__ import annotations

import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlparse
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import date, datetime, time as dt_time, timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import (
    Avg,
    BooleanField,
    Case,
    Count,
    DurationField,
    ExpressionWrapper,
    F,
    Max,
    Min,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
    Value,
    When,
)
from django.db.models.fields.json import KeyTextTransform
from django.utils import timezone

from apps.ai_engagement.models import (
    Chunk,
    Document,
    KnowledgeSource,
    OrgInfo,
)
from apps.analytics.models import AnalyticsSettings
from apps.ai_engagement.services.confidentiality import is_sensitive_attribute_definition
from apps.ai_engagement.services.organization_profile import compile_qualification_requirements
from apps.ai_engagement.services.playbook import (
    qualification_questions,
    validate_playbook,
)
from apps.ai_engagement.services.qualification_state import (
    QUALIFIED_STAGE,
    normalize_stage_name,
    requirements_for_lead,
    state_for_lead,
)
from apps.channels.campaign_models import CampaignDelivery
from apps.channels.instagram_models import InstagramMessage
from apps.channels.models import WhatsAppAccount, WhatsAppMessage, WhatsAppTemplate
from apps.crm.models import AttributeDefinition, Lead, LeadActivity, Pipeline, Stage
from apps.followups.models import (
    FollowupExecution,
    FollowupSequence,
    FollowupStep,
    LeadSequenceState,
)
from apps.hosted_automation.models import HostedAutomationJob
from apps.integrations.diagnostic_auth import sanitize_data, sanitize_text
from apps.integrations.diagnostic_tools import (
    DiagnosticToolError,
    execute_tool as execute_diagnostic_tool,
)
from apps.integrations.operations_approval import approval_fingerprint
from apps.integrations.operations_audit import organization_visible_audit_reason
from apps.integrations.operations_models import (
    OperationsApprovalUse,
    OperationsAuditEvent,
    OperationsSupportSession,
)
from apps.integrations.operations_presence import visible_support_sessions
from apps.integrations.operations_policy import (
    CAP_AI_CONFIG_WRITE,
    CAP_AUDIT_READ,
    CAP_ATTRIBUTE_CONFIG_WRITE,
    CAP_CADENCE_CONFIG_WRITE,
    CAP_MESSAGING_CONFIG_WRITE,
    CAP_PIPELINE_CONFIG_WRITE,
    CAP_STAGE_CONFIG_WRITE,
    CAP_WORKFLOW_CONFIG_WRITE,
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
from apps.organizations.access import organization_is_active
from apps.organizations.models import Organization
from apps.triggers.models import SmartTrigger, TriggerRun
from services.channels.hosted_whatsapp_service import (
    HostedWhatsAppValidationError,
    get_pipeline_for_account,
    get_session_settings,
    preview_session_settings_update,
    update_session_settings,
)
from services.crm.attribute_service import (
    MAX_CUSTOM_ATTRIBUTES,
    create_attribute_definition,
    update_attribute_definition,
    update_lead_attribute_values,
)
from apps.crm.services.stage_requirements import missing_attributes
from services.crm.lead_transition import (
    LeadTransitionError,
    move_lead_to_pipeline_stage,
)
from services.followup_service import (
    FollowupError,
    _validate_schedule,
    add_email_step,
    add_reminder_step,
    add_whatsapp_step,
    create_sequence,
    update_sequence,
)
from services.triggers.rules import validate as validate_workflow_rule

from apps.integrations.operations_tools import (
    _uuid,
    _organization_for,
    _write_gate,
    _proposal_digest,
    _ensure_approved_proposal_unchanged,
    _reject_secret_like_content,
    _incompatible_existing_attribute_value_count,
    _assert_workflow_safe_attribute_references,
    OperationsToolError,
    OperationsPermissionError,
    OperationsApprovalRequired,
    ToolExecution,
)

def upsert_pipeline_configuration(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_PIPELINE_CONFIG_WRITE,
        tool_name="upsert_pipeline_configuration",
        arguments=arguments,
    )
    data = (arguments or {}).get("data")
    if not isinstance(data, dict):
        raise OperationsToolError("data must be a pipeline configuration object.")
    _reject_secret_like_content(data, field="pipeline")

    pipeline_id = (arguments or {}).get("pipeline_id")
    pipeline = None
    if pipeline_id:
        pipeline = Pipeline.objects.filter(
            pk=_uuid(pipeline_id, field="pipeline_id"),
            organization=organization,
        ).first()
        if pipeline is None:
            raise OperationsToolError("Pipeline not found in the active organization.")

    name = str(data.get("name", pipeline.name if pipeline else "") or "").strip()
    description = str(
        data.get("description", pipeline.description if pipeline else "") or ""
    ).strip()
    if not name or len(name) > 150:
        raise OperationsToolError("Pipeline name is required and must be at most 150 characters.")
    if len(description) > 10000:
        raise OperationsToolError("Pipeline description is too large.")
    is_active = data.get("is_active", pipeline.is_active if pipeline else True)
    ai_enabled = data.get("ai_enabled", pipeline.ai_enabled if pipeline else True)
    if not isinstance(is_active, bool) or not isinstance(ai_enabled, bool):
        raise OperationsToolError("is_active and ai_enabled must be true or false.")
    if (
        pipeline is not None
        and pipeline.is_active
        and is_active is False
        and Lead.objects.filter(
            organization=organization,
            pipeline=pipeline,
        ).exists()
    ):
        raise OperationsPermissionError(
            "This pipeline still contains leads. Move those leads to another "
            "active pipeline before deactivating it."
        )
    duplicate = Pipeline.objects.filter(
        organization=organization,
        name__iexact=name,
    )
    if pipeline is not None:
        duplicate = duplicate.exclude(pk=pipeline.pk)
    if duplicate.exists():
        raise OperationsToolError("A pipeline with this name already exists.")

    before = (
        {
            "id": str(pipeline.id),
            "name": pipeline.name,
            "description": pipeline.description,
            "is_active": pipeline.is_active,
            "ai_enabled": pipeline.ai_enabled,
        }
        if pipeline
        else None
    )
    after = {
        "name": name,
        "description": description,
        "is_active": is_active,
        "ai_enabled": ai_enabled,
    }
    proposal = {
        "organization_id": str(organization.id),
        "pipeline_id": str(pipeline.id) if pipeline else None,
        "before": before,
        "after": after,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(
            arguments=arguments,
            proposal=proposal,
        )
    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "operation": "update" if pipeline else "create",
                "before": before,
                "after": after,
                "note": (
                    "A new pipeline automatically receives SHVYA's standard stages."
                    if pipeline is None
                    else None
                ),
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_PIPELINE_CONFIG_WRITE,
                ),
                "reversible": pipeline is not None,
            },
            capability=CAP_PIPELINE_CONFIG_WRITE,
            target_type="pipeline" if pipeline else "organization",
            target_id=str(pipeline.id) if pipeline else str(organization.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={
                "operation": "upsert_pipeline",
                "mode": "update" if pipeline else "create",
                "changed_fields": sorted(after),
                "proposal_digest": _proposal_digest(proposal),
            },
        )

    creating_pipeline = pipeline is None
    try:
        with transaction.atomic():
            Organization.objects.select_for_update().get(
                pk=organization.pk
            )
            if pipeline is not None:
                pipeline = (
                    Pipeline.objects.select_for_update()
                    .filter(
                        pk=pipeline.pk,
                        organization=organization,
                    )
                    .first()
                )
                if pipeline is None:
                    raise OperationsApprovalRequired(
                        "The pipeline changed or was removed after review. "
                        "Run a fresh dry-run."
                    )

            duplicate = Pipeline.objects.filter(
                organization=organization,
                name__iexact=name,
            )
            if pipeline is not None:
                duplicate = duplicate.exclude(pk=pipeline.pk)
            if duplicate.exists():
                raise OperationsToolError(
                    "A pipeline with this name already exists."
                )

            locked_before = (
                {
                    "id": str(pipeline.id),
                    "name": pipeline.name,
                    "description": pipeline.description,
                    "is_active": pipeline.is_active,
                    "ai_enabled": pipeline.ai_enabled,
                }
                if pipeline is not None
                else None
            )
            locked_proposal = {
                "organization_id": str(organization.id),
                "pipeline_id": (
                    str(pipeline.id)
                    if pipeline is not None
                    else None
                ),
                "before": locked_before,
                "after": after,
            }
            _ensure_approved_proposal_unchanged(
                arguments=arguments,
                proposal=locked_proposal,
            )

            if pipeline is None:
                pipeline = Pipeline(
                    organization=organization,
                    name=name,
                    description=description,
                    is_active=is_active,
                    ai_enabled=ai_enabled,
                )
            else:
                pipeline.name = name
                pipeline.description = description
                pipeline.is_active = is_active
                pipeline.ai_enabled = ai_enabled
            pipeline.full_clean()
            pipeline.save()

            if creating_pipeline:
                from apps.crm.models.signals import DEFAULT_PIPELINE_STAGES

                expected_stage_names = {
                    str(item["name"])
                    for item in DEFAULT_PIPELINE_STAGES
                }
                actual_stage_names = set(
                    Stage.objects.filter(
                        pipeline=pipeline,
                        is_active=True,
                    ).values_list("name", flat=True)
                )
                if not expected_stage_names.issubset(
                    actual_stage_names
                ):
                    raise OperationsToolError(
                        "Pipeline creation did not produce SHVYA's required "
                        "standard stages; the transaction was rolled back."
                    )
            pipeline.refresh_from_db()
            if (
                pipeline.name != name
                or pipeline.description != description
                or pipeline.is_active != is_active
                or pipeline.ai_enabled != ai_enabled
            ):
                raise OperationsToolError(
                    "Pipeline configuration verification failed."
                )
            before = locked_before
    except IntegrityError as exc:
        raise OperationsToolError(
            "Pipeline configuration changed concurrently. "
            "Run a fresh dry-run."
        ) from exc
    except ValidationError as exc:
        raise OperationsToolError(
            "Pipeline configuration validation failed."
        ) from exc

    return ToolExecution(
        data={
            "status": "FIXED",
            "pipeline": {
                "id": str(pipeline.id),
                "name": pipeline.name,
                "is_active": pipeline.is_active,
                "ai_enabled": pipeline.ai_enabled,
                "stage_count": pipeline.stages.count(),
            },
            "verification": "passed",
        },
        capability=CAP_PIPELINE_CONFIG_WRITE,
        target_type="pipeline",
        target_id=str(pipeline.id),
        reason=reason,
        audit_summary={
            "operation": "upsert_pipeline",
            "mode": "update" if before else "create",
            "changed_fields": sorted(after),
            "verification": "passed",
        },
    )
