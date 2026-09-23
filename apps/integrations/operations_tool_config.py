# ruff: noqa: F401
"""Pipeline, stage, attribute, workflow and Cadence configuration tools."""

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


def upsert_attribute_configuration(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_ATTRIBUTE_CONFIG_WRITE,
        tool_name="upsert_attribute_configuration",
        arguments=arguments,
    )
    data = (arguments or {}).get("data")
    if not isinstance(data, dict):
        raise OperationsToolError("data must be an attribute configuration object.")
    _reject_secret_like_content(data, field="attribute")
    attribute_id = (arguments or {}).get("attribute_id")
    attribute = None
    if attribute_id:
        attribute = AttributeDefinition.objects.filter(
            pk=_uuid(attribute_id, field="attribute_id"),
            organization=organization,
        ).first()
        if attribute is None:
            raise OperationsToolError("Attribute definition not found in this organization.")

    name = str(data.get("name", attribute.name if attribute else "") or "").strip()
    field_type = str(
        data.get(
            "field_type",
            attribute.field_type if attribute else AttributeDefinition.FieldType.TEXT,
        )
        or ""
    ).strip()
    description = str(
        data.get("description", attribute.description if attribute else "") or ""
    ).strip()
    options = data.get("options", list(attribute.options or []) if attribute else [])
    if not isinstance(options, list):
        raise OperationsToolError("Attribute options must be a list.")
    if field_type == AttributeDefinition.FieldType.OPTION:
        cleaned_options = []
        for item in options:
            value = str(item or "").strip()
            if value and value not in cleaned_options:
                cleaned_options.append(value)
        options = cleaned_options
    else:
        options = []
    if is_sensitive_attribute_definition({"key": name, "name": name}):
        raise OperationsPermissionError(
            "Credential-like or secret attribute definitions cannot be created through Operations MCP."
        )

    if attribute is not None and (
        field_type != attribute.field_type
        or list(options or []) != list(attribute.options or [])
    ):
        incompatible_count = _incompatible_existing_attribute_value_count(
            organization=organization,
            attribute=attribute,
            field_type=field_type,
            options=options,
        )
        if incompatible_count:
            raise OperationsPermissionError(
                "This attribute type/options change would make existing CRM "
                f"values invalid for {incompatible_count} lead(s). Update or "
                "clear those values first, then run a new dry-run."
            )

    before = (
        {
            "id": str(attribute.id),
            "key": attribute.key,
            "name": attribute.name,
            "field_type": attribute.field_type,
            "description": attribute.description,
            "options": attribute.options,
            "is_active": attribute.is_active,
        }
        if attribute
        else None
    )
    after = {
        "name": name,
        "field_type": field_type,
        "description": description,
        "options": options,
        "is_active": True,
    }
    proposal = {
        "organization_id": str(organization.id),
        "attribute_id": str(attribute.id) if attribute else None,
        "before": before,
        "after": after,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(
            arguments=arguments,
            proposal=proposal,
        )
    if dry_run:
        # Use a non-persisted instance for field-level validation while the
        # canonical service remains the write authority on execution.
        probe = AttributeDefinition(
            organization=organization,
            name=name,
            key=attribute.key if attribute else "operations_probe",
            field_type=field_type,
            description=description,
            options=options,
            display_order=attribute.display_order if attribute else 0,
        )
        try:
            probe.full_clean(validate_unique=False, validate_constraints=False)
        except ValidationError as exc:
            raise OperationsToolError("Attribute configuration validation failed.") from exc
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "operation": "update" if attribute else "create",
                "before": before,
                "after": after,
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_ATTRIBUTE_CONFIG_WRITE,
                ),
                "reversible": attribute is not None,
            },
            capability=CAP_ATTRIBUTE_CONFIG_WRITE,
            target_type="attribute" if attribute else "organization",
            target_id=str(attribute.id) if attribute else str(organization.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={
                "operation": "upsert_attribute",
                "mode": "update" if attribute else "create",
                "changed_fields": sorted(after),
                "proposal_digest": _proposal_digest(proposal),
            },
        )

    try:
        with transaction.atomic():
            Organization.objects.select_for_update().get(
                pk=organization.pk
            )
            if attribute is not None:
                attribute = (
                    AttributeDefinition.objects.select_for_update()
                    .filter(
                        pk=attribute.pk,
                        organization=organization,
                    )
                    .first()
                )
                if attribute is None:
                    raise OperationsApprovalRequired(
                        "The CRM attribute changed or was removed after review. "
                        "Run a fresh dry-run."
                    )

            duplicate_name = AttributeDefinition.objects.filter(
                organization=organization,
                name__iexact=name,
            )
            if attribute is not None:
                duplicate_name = duplicate_name.exclude(
                    pk=attribute.pk
                )
            if duplicate_name.exists():
                raise OperationsToolError(
                    "An attribute with this name already exists."
                )

            locked_before = (
                {
                    "id": str(attribute.id),
                    "key": attribute.key,
                    "name": attribute.name,
                    "field_type": attribute.field_type,
                    "description": attribute.description,
                    "options": attribute.options,
                    "is_active": attribute.is_active,
                }
                if attribute is not None
                else None
            )
            locked_after = {
                "name": name,
                "field_type": field_type,
                "description": description,
                "options": options,
                "is_active": True,
            }
            locked_proposal = {
                "organization_id": str(organization.id),
                "attribute_id": (
                    str(attribute.id)
                    if attribute is not None
                    else None
                ),
                "before": locked_before,
                "after": locked_after,
            }
            _ensure_approved_proposal_unchanged(
                arguments=arguments,
                proposal=locked_proposal,
            )

            if attribute is not None and (
                field_type != attribute.field_type
                or list(options or [])
                != list(attribute.options or [])
            ):
                incompatible_count = (
                    _incompatible_existing_attribute_value_count(
                        organization=organization,
                        attribute=attribute,
                        field_type=field_type,
                        options=options,
                    )
                )
                if incompatible_count:
                    raise OperationsPermissionError(
                        "This attribute type/options change would make existing "
                        f"CRM values invalid for {incompatible_count} lead(s). "
                        "Update or clear those values first, then run a new dry-run."
                    )

            if attribute is None:
                attribute = create_attribute_definition(
                    organization=organization,
                    name=name,
                    field_type=field_type,
                    description=description,
                    options=options,
                )
                if (
                    AttributeDefinition.objects.filter(
                        organization=organization,
                        is_active=True,
                    ).count()
                    > MAX_CUSTOM_ATTRIBUTES
                ):
                    raise OperationsToolError(
                        "Attribute limit changed concurrently; creation was rolled back."
                    )
            else:
                attribute = update_attribute_definition(
                    organization=organization,
                    attribute=attribute,
                    name=name,
                    field_type=field_type,
                    description=description,
                    options=options,
                )
            attribute.refresh_from_db()
            if (
                attribute.name != name
                or attribute.field_type != field_type
                or attribute.description != description
                or list(attribute.options or []) != list(options)
                or not attribute.is_active
            ):
                raise OperationsToolError(
                    "Attribute configuration verification failed."
                )
            before = locked_before
            after = locked_after
    except IntegrityError as exc:
        raise OperationsToolError(
            "Attribute configuration changed concurrently. "
            "Run a fresh dry-run."
        ) from exc
    except ValidationError as exc:
        raise OperationsToolError(
            "Attribute configuration validation failed."
        ) from exc
    return ToolExecution(
        data={
            "status": "FIXED",
            "attribute": {
                "id": str(attribute.id),
                "key": attribute.key,
                "name": attribute.name,
                "field_type": attribute.field_type,
                "options": attribute.options,
            },
            "verification": "passed",
        },
        capability=CAP_ATTRIBUTE_CONFIG_WRITE,
        target_type="attribute",
        target_id=str(attribute.id),
        reason=reason,
        audit_summary={
            "operation": "upsert_attribute",
            "mode": "update" if before else "create",
            "changed_fields": sorted(after),
            "verification": "passed",
        },
    )


def upsert_workflow_configuration(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_WORKFLOW_CONFIG_WRITE,
        tool_name="upsert_workflow_configuration",
        arguments=arguments,
    )
    data = (arguments or {}).get("data")
    if not isinstance(data, dict):
        raise OperationsToolError("data must be a Workflow configuration object.")
    _reject_secret_like_content(data, field="workflow")
    try:
        clean = validate_workflow_rule(organization, data)
    except ValidationError as exc:
        raise OperationsToolError("Workflow validation failed.") from exc
    _assert_workflow_safe_attribute_references(
        organization=organization,
        clean=clean,
    )

    workflow_id = (arguments or {}).get("workflow_id")
    workflow = None
    if workflow_id:
        workflow = SmartTrigger.objects.filter(
            pk=_uuid(workflow_id, field="workflow_id"),
            organization=organization,
        ).first()
        if workflow is None:
            raise OperationsToolError("Workflow not found in this organization.")

    duplicate = SmartTrigger.objects.filter(
        organization=organization,
        fingerprint=clean["fingerprint"],
    )
    if workflow is not None:
        duplicate = duplicate.exclude(pk=workflow.pk)
    if duplicate.exists():
        raise OperationsToolError("An identical Workflow already exists.")

    workflow_before = (
        {
            "id": str(workflow.id),
            "name": workflow.name,
            "enabled": workflow.enabled,
            "is_active": workflow.is_active,
            "position": workflow.position,
            "trigger_type": workflow.trigger_type,
            "conditions": workflow.conditions,
            "action_type": workflow.action_type,
            "action": workflow.action,
            "fingerprint": workflow.fingerprint,
        }
        if workflow is not None
        else None
    )
    proposed_position = (
        workflow.position
        if workflow is not None
        else (
            SmartTrigger.objects.filter(
                organization=organization,
                is_active=True,
            ).aggregate(value=Max("position"))["value"]
            or 0
        ) + 1
    )
    workflow_after = {
        **clean,
        "is_active": True,
        "position": proposed_position,
    }
    proposal = {
        "organization_id": str(organization.id),
        "workflow_id": str(workflow.id) if workflow else None,
        "before": workflow_before,
        "after": workflow_after,
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
                "operation": "update" if workflow else "create",
                "workflow": {
                    "name": clean["name"],
                    "trigger_type": clean["trigger_type"],
                    "action_type": clean["action_type"],
                    "enabled": clean["enabled"],
                    "scope_count": len((clean.get("conditions") or {}).get("scopes") or []),
                },
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_WORKFLOW_CONFIG_WRITE,
                ),
                "reversible": workflow is not None,
            },
            capability=CAP_WORKFLOW_CONFIG_WRITE,
            target_type="workflow" if workflow else "organization",
            target_id=str(workflow.id) if workflow else str(organization.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={
                "operation": "upsert_workflow",
                "mode": "update" if workflow else "create",
                "trigger_type": clean["trigger_type"],
                "action_type": clean["action_type"],
                "proposal_digest": _proposal_digest(proposal),
            },
        )

    try:
        with transaction.atomic():
            Organization.objects.select_for_update().get(
                pk=organization.pk
            )
            try:
                clean_locked = validate_workflow_rule(
                    organization,
                    data,
                )
            except ValidationError as exc:
                raise OperationsApprovalRequired(
                    "Workflow references changed after review. "
                    "Run a fresh dry-run."
                ) from exc
            _assert_workflow_safe_attribute_references(
                organization=organization,
                clean=clean_locked,
            )

            if workflow is not None:
                workflow = (
                    SmartTrigger.objects.select_for_update()
                    .filter(
                        pk=workflow.pk,
                        organization=organization,
                    )
                    .first()
                )
                if workflow is None:
                    raise OperationsApprovalRequired(
                        "The Workflow changed or was removed after review. "
                        "Run a fresh dry-run."
                    )

            duplicate = SmartTrigger.objects.filter(
                organization=organization,
                fingerprint=clean_locked["fingerprint"],
            )
            if workflow is not None:
                duplicate = duplicate.exclude(pk=workflow.pk)
            if duplicate.exists():
                raise OperationsToolError(
                    "An identical Workflow already exists."
                )

            proposed_position = (
                workflow.position
                if workflow is not None
                else (
                    SmartTrigger.objects.filter(
                        organization=organization,
                        is_active=True,
                    ).aggregate(value=Max("position"))["value"]
                    or 0
                ) + 1
            )
            locked_before = (
                {
                    "id": str(workflow.id),
                    "name": workflow.name,
                    "enabled": workflow.enabled,
                    "is_active": workflow.is_active,
                    "position": workflow.position,
                    "trigger_type": workflow.trigger_type,
                    "conditions": workflow.conditions,
                    "action_type": workflow.action_type,
                    "action": workflow.action,
                    "fingerprint": workflow.fingerprint,
                }
                if workflow is not None
                else None
            )
            locked_after = {
                **clean_locked,
                "is_active": True,
                "position": proposed_position,
            }
            locked_proposal = {
                "organization_id": str(organization.id),
                "workflow_id": (
                    str(workflow.id)
                    if workflow is not None
                    else None
                ),
                "before": locked_before,
                "after": locked_after,
            }
            _ensure_approved_proposal_unchanged(
                arguments=arguments,
                proposal=locked_proposal,
            )

            if workflow is None:
                workflow = SmartTrigger(
                    organization=organization,
                    created_by=identity.actor,
                    position=proposed_position,
                )
            for key, value in clean_locked.items():
                setattr(workflow, key, value)
            workflow.is_active = True
            workflow.save()
            clean = clean_locked
            workflow.refresh_from_db()
            if (
                workflow.fingerprint != clean["fingerprint"]
                or workflow.trigger_type != clean["trigger_type"]
                or workflow.action_type != clean["action_type"]
                or workflow.enabled != clean["enabled"]
                or not workflow.is_active
            ):
                raise OperationsToolError(
                    "Workflow configuration verification failed."
                )
    except IntegrityError as exc:
        raise OperationsToolError(
            "Workflow configuration changed concurrently. "
            "Run a fresh dry-run."
        ) from exc
    return ToolExecution(
        data={
            "status": "FIXED",
            "workflow": {
                "id": str(workflow.id),
                "name": workflow.name,
                "trigger_type": workflow.trigger_type,
                "action_type": workflow.action_type,
                "enabled": workflow.enabled,
            },
            "verification": "passed",
        },
        capability=CAP_WORKFLOW_CONFIG_WRITE,
        target_type="workflow",
        target_id=str(workflow.id),
        reason=reason,
        audit_summary={
            "operation": "upsert_workflow",
            "mode": "update" if workflow_id else "create",
            "trigger_type": workflow.trigger_type,
            "action_type": workflow.action_type,
            "verification": "passed",
        },
    )


def _cadence_schedule(step_data):
    schedule = step_data.get("schedule") or {}
    if not isinstance(schedule, dict):
        raise OperationsToolError("Cadence step schedule must be an object.")
    schedule_type = str(
        schedule.get("type") or FollowupStep.ScheduleType.IMMEDIATE
    ).strip()
    specific_time = schedule.get("time")
    if specific_time:
        try:
            specific_time = dt_time.fromisoformat(str(specific_time))
        except ValueError as exc:
            raise OperationsToolError("Cadence time must use HH:MM or HH:MM:SS.") from exc
    try:
        delay_value = (
            int(schedule["delay_value"])
            if schedule.get("delay_value") is not None
            else None
        )
        recurring_every = (
            int(schedule["recurring_every"])
            if schedule.get("recurring_every") is not None
            else None
        )
        specific_weekday = (
            int(schedule["weekday"])
            if schedule.get("weekday") is not None
            else None
        )
        recurring_weekdays = [
            int(item) for item in (schedule.get("weekdays") or [])
        ]
    except (TypeError, ValueError) as exc:
        raise OperationsToolError("Cadence numeric schedule values are invalid.") from exc
    payload = {
        "schedule_type": schedule_type,
        "delay_value": delay_value,
        "delay_unit": str(schedule.get("delay_unit") or ""),
        "specific_time": specific_time,
        "specific_weekday": specific_weekday,
        "recurring_every": recurring_every,
        "recurring_unit": str(schedule.get("recurring_unit") or ""),
        "recurring_weekdays": recurring_weekdays,
    }
    try:
        _validate_schedule(**payload)
    except FollowupError as exc:
        raise OperationsToolError(str(exc)) from exc
    return payload


def upsert_cadence_configuration(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_CADENCE_CONFIG_WRITE,
        tool_name="upsert_cadence_configuration",
        arguments=arguments,
    )
    data = (arguments or {}).get("data")
    if not isinstance(data, dict):
        raise OperationsToolError("data must be a Cadence configuration object.")
    _reject_secret_like_content(data, field="cadence")
    sequence_id = (arguments or {}).get("cadence_id")
    sequence = None
    if sequence_id:
        sequence = (
            FollowupSequence.objects.filter(
                pk=_uuid(sequence_id, field="cadence_id"),
                organization=organization,
            )
            .select_related("whatsapp_account")
            .defer("whatsapp_account__access_token")
            .first()
        )
        if sequence is None:
            raise OperationsToolError("Cadence not found in this organization.")

    name = str(data.get("name", sequence.name if sequence else "") or "").strip()
    description = str(
        data.get("description", sequence.description if sequence else "") or ""
    ).strip()
    is_active = data.get(
        "is_active",
        sequence.is_active if sequence is not None else True,
    )
    if not isinstance(is_active, bool):
        raise OperationsToolError("Cadence is_active must be true or false.")
    if not name or len(name) > 255 or len(description) > 300:
        raise OperationsToolError("Cadence name/description is invalid.")
    duplicate = FollowupSequence.objects.filter(
        organization=organization,
        name__iexact=name,
    )
    if sequence is not None:
        duplicate = duplicate.exclude(pk=sequence.pk)
    if duplicate.exists():
        raise OperationsToolError("A Cadence with this name already exists.")

    account = sequence.whatsapp_account if sequence else None
    if sequence is not None:
        existing_provider = (
            "api"
            if account.connection_type == WhatsAppAccount.ConnectionType.API
            else "hosted"
        )
        requested_provider = data.get("provider")
        if (
            requested_provider is not None
            and str(requested_provider).strip() != existing_provider
        ):
            raise OperationsPermissionError(
                "Cadence provider cannot be changed on an existing Cadence "
                "through the canonical SHVYA service. Create a new Cadence "
                "for a different provider."
            )
        requested_account_id = data.get("whatsapp_account_id")
        if (
            requested_account_id is not None
            and str(requested_account_id).strip() != str(sequence.whatsapp_account_id)
        ):
            raise OperationsPermissionError(
                "Cadence WhatsApp sender cannot be changed on an existing "
                "Cadence through the canonical SHVYA service. Create a new "
                "Cadence for a different sender."
            )
        provider = existing_provider
    else:
        provider = str(data.get("provider") or "api").strip()

    if provider not in {"api", "hosted"}:
        raise OperationsToolError("Cadence provider must be api or hosted.")
    if sequence is None:
        account_id = data.get("whatsapp_account_id")
        if account_id:
            account = (
                WhatsAppAccount.objects.filter(
                    pk=_uuid(account_id, field="whatsapp_account_id"),
                    organization=organization,
                    is_active=True,
                    status=WhatsAppAccount.Status.CONNECTED,
                )
                .defer("access_token")
                .first()
            )
        if provider == "api" and account is None:
            raise OperationsToolError("An active connected WhatsApp API account is required.")
        if account is not None and provider == "api" and account.connection_type != WhatsAppAccount.ConnectionType.API:
            raise OperationsToolError("The selected account is not a WhatsApp API account.")
        if provider == "hosted":
            if (
                account is not None
                and account.connection_type
                != WhatsAppAccount.ConnectionType.coexisted
            ):
                raise OperationsToolError(
                    "The selected account is not a Hosted/Coexistence WhatsApp account."
                )
            if account is None:
                account = (
                    WhatsAppAccount.objects.filter(
                        organization=organization,
                        connection_type=WhatsAppAccount.ConnectionType.coexisted,
                        status=WhatsAppAccount.Status.CONNECTED,
                        is_active=True,
                    )
                    .defer("access_token")
                    .order_by(
                        "business_name",
                        "display_phone_number",
                    )
                    .first()
                )
            if account is None:
                raise OperationsToolError(
                    "Connect at least one Hosted/Coexistence WhatsApp number before creating this Cadence."
                )

    cadence_before = (
        {
            "id": str(sequence.id),
            "name": sequence.name,
            "description": sequence.description,
            "provider": provider,
            "whatsapp_account_id": str(sequence.whatsapp_account_id),
            "is_active": sequence.is_active,
        }
        if sequence is not None
        else None
    )
    cadence_after = {
        "name": name,
        "description": description,
        "provider": provider,
        "whatsapp_account_id": str(account.id) if account else None,
        "is_active": is_active,
    }
    proposal = {
        "organization_id": str(organization.id),
        "cadence_id": str(sequence.id) if sequence else None,
        "before": cadence_before,
        "after": cadence_after,
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
                "operation": "update" if sequence else "create",
                "cadence": {
                    "name": name,
                    "description": description,
                    "provider": provider,
                    "whatsapp_account_id": str(account.id) if account else None,
                },
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_CADENCE_CONFIG_WRITE,
                ),
                "reversible": sequence is not None,
            },
            capability=CAP_CADENCE_CONFIG_WRITE,
            target_type="cadence" if sequence else "organization",
            target_id=str(sequence.id) if sequence else str(organization.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={
                "operation": "upsert_cadence",
                "mode": "update" if sequence else "create",
                "proposal_digest": _proposal_digest(proposal),
            },
        )

    try:
        with transaction.atomic():
            Organization.objects.select_for_update().get(
                pk=organization.pk
            )
            if sequence is not None:
                sequence = (
                    FollowupSequence.objects.select_for_update()
                    .select_related("whatsapp_account")
                    .defer("whatsapp_account__access_token")
                    .filter(
                        pk=sequence.pk,
                        organization=organization,
                    )
                    .first()
                )
                if sequence is None:
                    raise OperationsApprovalRequired(
                        "The Cadence changed or was removed after review. "
                        "Run a fresh dry-run."
                    )
                account = sequence.whatsapp_account
                locked_provider = (
                    "api"
                    if account.connection_type
                    == WhatsAppAccount.ConnectionType.API
                    else "hosted"
                )
                if locked_provider != provider:
                    raise OperationsApprovalRequired(
                        "The Cadence provider changed after review. "
                        "Run a fresh dry-run."
                    )
            else:
                if provider == "api":
                    account_id = data.get("whatsapp_account_id")
                    account = (
                        WhatsAppAccount.objects.filter(
                            pk=_uuid(
                                account_id,
                                field="whatsapp_account_id",
                            ),
                            organization=organization,
                            is_active=True,
                            status=WhatsAppAccount.Status.CONNECTED,
                            connection_type=WhatsAppAccount.ConnectionType.API,
                        )
                        .defer("access_token")
                        .first()
                        if account_id
                        else None
                    )
                    if account is None:
                        raise OperationsApprovalRequired(
                            "The selected WhatsApp API sender is no longer "
                            "active/connected. Run a fresh dry-run."
                        )
                else:
                    account_id = data.get("whatsapp_account_id")
                    account = (
                        WhatsAppAccount.objects.filter(
                            pk=_uuid(
                                account_id,
                                field="whatsapp_account_id",
                            ),
                            organization=organization,
                            connection_type=WhatsAppAccount.ConnectionType.coexisted,
                            status=WhatsAppAccount.Status.CONNECTED,
                            is_active=True,
                        )
                        .defer("access_token")
                        .first()
                        if account_id
                        else None
                    )
                    if account is None:
                        account = (
                            WhatsAppAccount.objects.filter(
                                organization=organization,
                                connection_type=WhatsAppAccount.ConnectionType.coexisted,
                                status=WhatsAppAccount.Status.CONNECTED,
                                is_active=True,
                            )
                            .defer("access_token")
                            .order_by(
                                "business_name",
                                "display_phone_number",
                            )
                            .first()
                        )
                    if account is None:
                        raise OperationsApprovalRequired(
                            "No active Hosted/Coexistence sender is available. "
                            "Run a fresh dry-run."
                        )

            duplicate = FollowupSequence.objects.filter(
                organization=organization,
                name__iexact=name,
            )
            if sequence is not None:
                duplicate = duplicate.exclude(pk=sequence.pk)
            if duplicate.exists():
                raise OperationsToolError(
                    "A Cadence with this name already exists."
                )

            locked_before = (
                {
                    "id": str(sequence.id),
                    "name": sequence.name,
                    "description": sequence.description,
                    "provider": provider,
                    "whatsapp_account_id": str(
                        sequence.whatsapp_account_id
                    ),
                    "is_active": sequence.is_active,
                }
                if sequence is not None
                else None
            )
            locked_after = {
                "name": name,
                "description": description,
                "provider": provider,
                "whatsapp_account_id": (
                    str(account.id) if account else None
                ),
                "is_active": is_active,
            }
            locked_proposal = {
                "organization_id": str(organization.id),
                "cadence_id": (
                    str(sequence.id)
                    if sequence is not None
                    else None
                ),
                "before": locked_before,
                "after": locked_after,
            }
            _ensure_approved_proposal_unchanged(
                arguments=arguments,
                proposal=locked_proposal,
            )

            if sequence is None:
                sequence = create_sequence(
                    organization=organization,
                    created_by=identity.actor,
                    name=name,
                    description=description,
                    whatsapp_account=account,
                    provider=provider,
                )
            else:
                sequence = update_sequence(
                    sequence=sequence,
                    name=name,
                    description=description,
                )

            if sequence.is_active != is_active:
                sequence.is_active = is_active
                sequence.save(update_fields=["is_active", "updated_at"])

            sequence.refresh_from_db()
            if (
                sequence.name != name
                or sequence.description != description
                or sequence.whatsapp_account_id != account.id
                or sequence.is_active != is_active
            ):
                raise OperationsToolError(
                    "Cadence configuration verification failed."
                )
            cadence_before = locked_before
            cadence_after = locked_after
    except FollowupError as exc:
        raise OperationsToolError(str(exc)) from exc
    except IntegrityError as exc:
        raise OperationsToolError(
            "Cadence configuration changed concurrently. "
            "Run a fresh dry-run."
        ) from exc
    return ToolExecution(
        data={
            "status": "FIXED",
            "cadence": {
                "id": str(sequence.id),
                "name": sequence.name,
                "step_count": sequence.steps.count(),
                "is_active": sequence.is_active,
            },
            "verification": "passed",
        },
        capability=CAP_CADENCE_CONFIG_WRITE,
        target_type="cadence",
        target_id=str(sequence.id),
        reason=reason,
        audit_summary={
            "operation": "upsert_cadence",
            "mode": "update" if cadence_before else "create",
            "verification": "passed",
        },
    )


def add_cadence_step(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_CADENCE_CONFIG_WRITE,
        tool_name="add_cadence_step",
        arguments=arguments,
    )
    sequence = (
        FollowupSequence.objects.filter(
            pk=_uuid((arguments or {}).get("cadence_id"), field="cadence_id"),
            organization=organization,
            is_active=True,
        )
        .select_related("whatsapp_account")
        .defer("whatsapp_account__access_token")
        .first()
    )
    if sequence is None:
        raise OperationsToolError("Active Cadence not found in this organization.")
    data = (arguments or {}).get("data")
    if not isinstance(data, dict):
        raise OperationsToolError("data must be a Cadence step object.")
    _reject_secret_like_content(data, field="cadence_step")
    step_type = str(data.get("type") or "").strip().lower()
    if step_type not in {"whatsapp", "email", "reminder"}:
        raise OperationsToolError("Cadence step type must be whatsapp, email, or reminder.")
    schedule = _cadence_schedule(data)

    template = None
    if step_type == "whatsapp":
        template_id = data.get("template_id")
        template = WhatsAppTemplate.objects.filter(
            pk=_uuid(template_id, field="template_id"),
            organization=organization,
            account=sequence.whatsapp_account,
            status=WhatsAppTemplate.Status.APPROVED,
        ).first()
        if template is None:
            raise OperationsToolError("Approved WhatsApp template not found for this Cadence account.")
    elif step_type == "email":
        if not str(data.get("subject") or "").strip() or not str(data.get("body") or "").strip():
            raise OperationsToolError("Email Cadence steps require subject and body.")
    else:
        if not str(data.get("text") or "").strip():
            raise OperationsToolError("Reminder Cadence steps require reminder text.")

    step_proposal = {
        "cadence_id": str(sequence.id),
        "existing_step_count": sequence.steps.count(),
        "next_position": sequence.steps.count() + 1,
        "step_type": step_type,
        "schedule": schedule,
        "template_id": str(template.id) if template else None,
        "title": str(data.get("title") or ""),
        "subject": str(data.get("subject") or ""),
        "body": str(data.get("body") or ""),
        "text": str(data.get("text") or ""),
        "retry_count": data.get("retry_count", 0),
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(
            arguments=arguments,
            proposal=step_proposal,
        )

    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "cadence_id": str(sequence.id),
                "step_type": step_type,
                "next_position": sequence.steps.count() + 1,
                "schedule_type": schedule["schedule_type"],
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_CADENCE_CONFIG_WRITE,
                ),
                "reversible": True,
            },
            capability=CAP_CADENCE_CONFIG_WRITE,
            target_type="cadence",
            target_id=str(sequence.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={
                "operation": "add_cadence_step",
                "step_type": step_type,
                "schedule_type": schedule["schedule_type"],
                "proposal_digest": _proposal_digest(step_proposal),
            },
        )

    try:
        with transaction.atomic():
            sequence = (
                FollowupSequence.objects.select_for_update()
                .select_related("whatsapp_account")
                .defer("whatsapp_account__access_token")
                .filter(
                    pk=sequence.pk,
                    organization=organization,
                    is_active=True,
                )
                .first()
            )
            if sequence is None:
                raise OperationsApprovalRequired(
                    "The Cadence changed, was removed, or became inactive. "
                    "Run a fresh dry-run."
                )

            schedule = _cadence_schedule(data)
            template = None
            if step_type == "whatsapp":
                template = WhatsAppTemplate.objects.filter(
                    pk=_uuid(
                        data.get("template_id"),
                        field="template_id",
                    ),
                    organization=organization,
                    account=sequence.whatsapp_account,
                    status=WhatsAppTemplate.Status.APPROVED,
                ).first()
                if template is None:
                    raise OperationsApprovalRequired(
                        "The approved WhatsApp template or Cadence sender "
                        "changed after review. Run a fresh dry-run."
                    )
            elif step_type == "email":
                if (
                    not str(data.get("subject") or "").strip()
                    or not str(data.get("body") or "").strip()
                ):
                    raise OperationsToolError(
                        "Email Cadence steps require subject and body."
                    )
            else:
                if not str(data.get("text") or "").strip():
                    raise OperationsToolError(
                        "Reminder Cadence steps require reminder text."
                    )

            current_step_count = sequence.steps.count()
            locked_step_proposal = {
                "cadence_id": str(sequence.id),
                "existing_step_count": current_step_count,
                "next_position": current_step_count + 1,
                "step_type": step_type,
                "schedule": schedule,
                "template_id": (
                    str(template.id) if template else None
                ),
                "title": str(data.get("title") or ""),
                "subject": str(data.get("subject") or ""),
                "body": str(data.get("body") or ""),
                "text": str(data.get("text") or ""),
                "retry_count": data.get("retry_count", 0),
            }
            _ensure_approved_proposal_unchanged(
                arguments=arguments,
                proposal=locked_step_proposal,
            )

            if step_type == "whatsapp":
                step = add_whatsapp_step(
                    sequence=sequence,
                    template=template,
                    retry_count=data.get("retry_count", 0),
                    **schedule,
                )
            elif step_type == "email":
                step = add_email_step(
                    sequence=sequence,
                    title=str(data.get("title") or ""),
                    subject=str(data.get("subject") or ""),
                    body=str(data.get("body") or ""),
                    **schedule,
                )
            else:
                step = add_reminder_step(
                    sequence=sequence,
                    text=str(data.get("text") or ""),
                    **schedule,
                )

            step.refresh_from_db()
            verification_errors = []
            if step.sequence_id != sequence.id:
                verification_errors.append("sequence")
            if step.step_type != step_type:
                verification_errors.append("type")
            if step.position != locked_step_proposal["next_position"]:
                verification_errors.append("position")
            if step.schedule_type != schedule["schedule_type"]:
                verification_errors.append("schedule_type")
            if step.delay_value != schedule["delay_value"]:
                verification_errors.append("delay_value")
            if step.delay_unit != schedule["delay_unit"]:
                verification_errors.append("delay_unit")
            if step.specific_time != schedule["specific_time"]:
                verification_errors.append("specific_time")
            if step.specific_weekday != schedule["specific_weekday"]:
                verification_errors.append("specific_weekday")
            if step.recurring_every != schedule["recurring_every"]:
                verification_errors.append("recurring_every")
            if step.recurring_unit != schedule["recurring_unit"]:
                verification_errors.append("recurring_unit")
            if list(step.recurring_weekdays or []) != list(
                schedule["recurring_weekdays"] or []
            ):
                verification_errors.append("recurring_weekdays")

            if step_type == "whatsapp":
                expected_retry = int(
                    data.get("retry_count", 0) or 0
                )
                if step.whatsapp_template_id != template.id:
                    verification_errors.append("template")
                if step.retry_count != expected_retry:
                    verification_errors.append("retry_count")
            elif step_type == "email":
                expected_title = (
                    str(data.get("title") or "").strip()
                    or f"Email {step.position}"
                )
                if step.title != expected_title:
                    verification_errors.append("title")
                if (
                    step.email_subject
                    != str(data.get("subject") or "").strip()
                ):
                    verification_errors.append("email_subject")
                if (
                    step.email_body
                    != str(data.get("body") or "").strip()
                ):
                    verification_errors.append("email_body")
            else:
                if (
                    step.reminder_text
                    != str(data.get("text") or "").strip()
                ):
                    verification_errors.append("reminder_text")

            if verification_errors:
                raise OperationsToolError(
                    "Cadence step verification failed for: "
                    + ", ".join(
                        sorted(set(verification_errors))
                    )
                )
            step_proposal = locked_step_proposal
    except FollowupError as exc:
        raise OperationsToolError(str(exc)) from exc
    except IntegrityError as exc:
        raise OperationsToolError(
            "Cadence steps changed concurrently. Run a fresh dry-run."
        ) from exc

    return ToolExecution(
        data={
            "status": "FIXED",
            "cadence_id": str(sequence.id),
            "step": {
                "id": str(step.id),
                "type": step.step_type,
                "position": step.position,
                "schedule_type": step.schedule_type,
            },
            "verification": "passed",
        },
        capability=CAP_CADENCE_CONFIG_WRITE,
        target_type="cadence_step",
        target_id=str(step.id),
        reason=reason,
        audit_summary={
            "operation": "add_cadence_step",
            "cadence_id": str(sequence.id),
            "step_type": step_type,
            "schedule_type": step.schedule_type,
            "verification": "passed",
        },
    )


