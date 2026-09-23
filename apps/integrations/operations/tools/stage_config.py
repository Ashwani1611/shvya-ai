# ruff: noqa: F401
"""Focused Operations MCP configuration tools: stage config."""

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

def upsert_stage_configuration(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_STAGE_CONFIG_WRITE,
        tool_name="upsert_stage_configuration",
        arguments=arguments,
    )
    pipeline = Pipeline.objects.filter(
        pk=_uuid((arguments or {}).get("pipeline_id"), field="pipeline_id"),
        organization=organization,
        is_active=True,
    ).first()
    if pipeline is None:
        raise OperationsToolError("Active pipeline not found in this organization.")

    data = (arguments or {}).get("data")
    if not isinstance(data, dict):
        raise OperationsToolError("data must be a stage configuration object.")
    _reject_secret_like_content(data, field="stage")
    stage_id = (arguments or {}).get("stage_id")
    stage = None
    if stage_id:
        stage = Stage.objects.filter(
            pk=_uuid(stage_id, field="stage_id"),
            pipeline=pipeline,
        ).first()
        if stage is None:
            raise OperationsToolError("Stage not found in the selected pipeline.")

    name = str(data.get("name", stage.name if stage else "") or "").strip()
    description = str(
        data.get("description", stage.description if stage else "") or ""
    ).strip()
    if not name or len(name) > 100:
        raise OperationsToolError("Stage name is required and must be at most 100 characters.")
    if len(description) > 1200:
        raise OperationsToolError("Stage description must be 1200 characters or fewer.")
    if stage is not None and stage.is_name_locked and name != stage.name:
        raise OperationsPermissionError("SHVYA protected stages cannot be renamed.")
    is_active = data.get("is_active", stage.is_active if stage else True)
    ai_on = data.get("ai_on", stage.ai_on if stage else True)
    if stage is not None and stage.is_system_locked and is_active is False:
        raise OperationsPermissionError("SHVYA protected stages cannot be deactivated.")
    if (
        stage is not None
        and is_active is False
        and Lead.objects.filter(
            organization=organization,
            pipeline=pipeline,
            stage=stage,
        ).exists()
    ):
        raise OperationsPermissionError(
            "This stage still contains leads. Move those leads to another "
            "stage before deactivating it."
        )
    if not isinstance(is_active, bool) or not isinstance(ai_on, bool):
        raise OperationsToolError("is_active and ai_on must be true or false.")

    if "display_order" in data:
        try:
            display_order = int(data["display_order"])
        except (TypeError, ValueError) as exc:
            raise OperationsToolError("display_order must be a non-negative integer.") from exc
        if display_order < 0:
            raise OperationsToolError("display_order must be a non-negative integer.")
    elif stage is not None:
        display_order = stage.display_order
    else:
        display_order = (
            Stage.objects.filter(pipeline=pipeline).aggregate(value=Max("display_order"))["value"]
            or 0
        ) + 1

    duplicate_name = Stage.objects.filter(
        pipeline=pipeline,
        name__iexact=name,
        is_active=True,
    )
    duplicate_order = Stage.objects.filter(
        pipeline=pipeline,
        display_order=display_order,
    )
    if stage is not None:
        duplicate_name = duplicate_name.exclude(pk=stage.pk)
        duplicate_order = duplicate_order.exclude(pk=stage.pk)
    if is_active and duplicate_name.exists():
        raise OperationsToolError("An active stage with this name already exists.")
    if duplicate_order.exists():
        raise OperationsToolError("Another stage already uses this display order.")

    before = (
        {
            "id": str(stage.id),
            "name": stage.name,
            "description": stage.description,
            "display_order": stage.display_order,
            "is_active": stage.is_active,
            "ai_on": stage.ai_on,
        }
        if stage
        else None
    )
    after = {
        "name": name,
        "description": description,
        "display_order": display_order,
        "is_active": is_active,
        "ai_on": ai_on,
    }
    proposal = {
        "pipeline_id": str(pipeline.id),
        "stage_id": str(stage.id) if stage else None,
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
                "operation": "update" if stage else "create",
                "before": before,
                "after": after,
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_STAGE_CONFIG_WRITE,
                ),
                "reversible": stage is not None,
            },
            capability=CAP_STAGE_CONFIG_WRITE,
            target_type="stage" if stage else "pipeline",
            target_id=str(stage.id) if stage else str(pipeline.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={
                "operation": "upsert_stage",
                "mode": "update" if stage else "create",
                "pipeline_id": str(pipeline.id),
                "changed_fields": sorted(after),
                "proposal_digest": _proposal_digest(proposal),
            },
        )

    try:
        with transaction.atomic():
            pipeline = (
                Pipeline.objects.select_for_update()
                .filter(
                    pk=pipeline.pk,
                    organization=organization,
                    is_active=True,
                )
                .first()
            )
            if pipeline is None:
                raise OperationsApprovalRequired(
                    "The target pipeline changed or became inactive. "
                    "Run a fresh dry-run."
                )
            if stage is not None:
                stage = (
                    Stage.objects.select_for_update()
                    .filter(
                        pk=stage.pk,
                        pipeline=pipeline,
                    )
                    .first()
                )
                if stage is None:
                    raise OperationsApprovalRequired(
                        "The stage changed or was removed after review. "
                        "Run a fresh dry-run."
                    )
            elif "display_order" not in data:
                display_order = (
                    Stage.objects.filter(
                        pipeline=pipeline
                    )
                    .aggregate(value=Max("display_order"))["value"]
                    or 0
                ) + 1

            duplicate_name = Stage.objects.filter(
                pipeline=pipeline,
                name__iexact=name,
                is_active=True,
            )
            duplicate_order = Stage.objects.filter(
                pipeline=pipeline,
                display_order=display_order,
            )
            if stage is not None:
                duplicate_name = duplicate_name.exclude(pk=stage.pk)
                duplicate_order = duplicate_order.exclude(pk=stage.pk)
            if is_active and duplicate_name.exists():
                raise OperationsToolError(
                    "An active stage with this name already exists."
                )
            if duplicate_order.exists():
                raise OperationsToolError(
                    "Another stage already uses this display order."
                )

            locked_before = (
                {
                    "id": str(stage.id),
                    "name": stage.name,
                    "description": stage.description,
                    "display_order": stage.display_order,
                    "is_active": stage.is_active,
                    "ai_on": stage.ai_on,
                }
                if stage is not None
                else None
            )
            locked_after = {
                "name": name,
                "description": description,
                "display_order": display_order,
                "is_active": is_active,
                "ai_on": ai_on,
            }
            locked_proposal = {
                "pipeline_id": str(pipeline.id),
                "stage_id": (
                    str(stage.id)
                    if stage is not None
                    else None
                ),
                "before": locked_before,
                "after": locked_after,
            }
            _ensure_approved_proposal_unchanged(
                arguments=arguments,
                proposal=locked_proposal,
            )

            if stage is None:
                stage = Stage(pipeline=pipeline)
            stage.name = name
            stage.description = description
            stage.display_order = display_order
            stage.is_active = is_active
            stage.ai_on = ai_on
            stage.full_clean()
            stage.save()
            stage.refresh_from_db()
            if (
                stage.name != name
                or stage.description != description
                or stage.display_order != display_order
                or stage.is_active != is_active
                or stage.ai_on != ai_on
            ):
                raise OperationsToolError(
                    "Stage configuration verification failed."
                )
            before = locked_before
            after = locked_after
    except IntegrityError as exc:
        raise OperationsToolError(
            "Stage configuration changed concurrently. "
            "Run a fresh dry-run."
        ) from exc
    except ValidationError as exc:
        raise OperationsToolError(
            "Stage configuration validation failed."
        ) from exc
    return ToolExecution(
        data={
            "status": "FIXED",
            "stage": {
                "id": str(stage.id),
                "pipeline_id": str(pipeline.id),
                "name": stage.name,
                "display_order": stage.display_order,
                "is_active": stage.is_active,
                "ai_on": stage.ai_on,
            },
            "verification": "passed",
        },
        capability=CAP_STAGE_CONFIG_WRITE,
        target_type="stage",
        target_id=str(stage.id),
        reason=reason,
        audit_summary={
            "operation": "upsert_stage",
            "mode": "update" if before else "create",
            "pipeline_id": str(pipeline.id),
            "changed_fields": sorted(after),
            "verification": "passed",
        },
    )
