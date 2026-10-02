# ruff: noqa: F401
"""Focused Operations MCP configuration tools: attribute config."""

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
                    )
                    .exclude(key="booked_at")
                    .count()
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
