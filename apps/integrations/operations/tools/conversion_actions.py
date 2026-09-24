# ruff: noqa: F401
"""Focused Operations MCP actions: conversion actions."""

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
    _reason,
    _require_operations_capability,
    _write_gate,
    _proposal_digest,
    _ensure_approved_proposal_unchanged,
    _tenant_safe_leads,
    _lead,
    _lead_for_write,
    _reject_secret_like_content,
    _normalized_lead_attribute_values,
    _validated_lead_attribute_values,
    _attribute_schema_snapshot,
    _qualification_contract_snapshot,
    _messaging_account,
    _public_messaging_settings,
    OperationsToolError,
    OperationsPermissionError,
    OperationsManualFixRequired,
    OperationsApprovalRequired,
    ToolExecution,
    MESSAGING_AUTOMATION_SETTING_KEYS,
    CONVERSION_BREAKDOWN_LIMIT,
    LOST_REASON_BREAKDOWN_LIMIT,
)

def get_conversion_analysis(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_DIAGNOSTICS_READ,
    )

    try:
        days = int((arguments or {}).get("days") or 30)
    except (TypeError, ValueError):
        days = 30
    days = max(7, min(days, 90))
    now = timezone.now()
    current_start = now - timedelta(days=days)
    previous_start = current_start - timedelta(days=days)
    tenant_lead_ids = _tenant_safe_leads(
        organization
    ).values("id")

    analytics_settings = (
        AnalyticsSettings.objects.select_related(
            "lead_won_stage__pipeline",
            "lead_lost_stage__pipeline",
        )
        .filter(organization=organization)
        .first()
    )
    configured_won_stage = (
        analytics_settings.lead_won_stage
        if (
            analytics_settings
            and analytics_settings.lead_won_stage_id
            and analytics_settings.lead_won_stage.pipeline.organization_id
            == organization.id
        )
        else None
    )
    configured_lost_stage = (
        analytics_settings.lead_lost_stage
        if (
            analytics_settings
            and analytics_settings.lead_lost_stage_id
            and analytics_settings.lead_lost_stage.pipeline.organization_id
            == organization.id
        )
        else None
    )
    stall_day_threshold = max(
        1,
        min(
            int(
                getattr(
                    analytics_settings,
                    "stall_day_threshold",
                    7,
                )
                or 7
            ),
            90,
        ),
    )

    lost_reason_definition = (
        AttributeDefinition.objects.filter(
            organization=organization,
            is_active=True,
        )
        .filter(
            Q(key__in=["lost_reason", "loss_reason"])
            | Q(name__iexact="Lost Reason")
            | Q(name__iexact="Loss Reason")
        )
        .order_by("display_order", "created_at")
        .first()
    )
    if (
        lost_reason_definition is not None
        and is_sensitive_attribute_definition(
            {
                "key": lost_reason_definition.key,
                "name": lost_reason_definition.name,
            }
        )
    ):
        lost_reason_definition = None

    def first_response_metrics(
        *,
        created,
        channel,
        start_at,
        end_at,
    ):
        if channel == "whatsapp":
            inbound = (
                WhatsAppMessage.objects.filter(
                    organization=organization,
                    account__organization=organization,
                    lead_id=OuterRef("pk"),
                    direction=WhatsAppMessage.Direction.INBOUND,
                    created_at__gte=start_at,
                    created_at__lt=end_at,
                )
                .order_by("created_at", "id")
                .values("created_at")[:1]
            )
            cohort = created.annotate(
                _first_inbound_at=Subquery(inbound)
            )
            outbound = (
                WhatsAppMessage.objects.filter(
                    organization=organization,
                    account__organization=organization,
                    lead_id=OuterRef("pk"),
                    direction=WhatsAppMessage.Direction.OUTBOUND,
                    created_at__gte=OuterRef(
                        "_first_inbound_at"
                    ),
                    created_at__lt=end_at,
                )
                .order_by("created_at", "id")
                .values("created_at")[:1]
            )
        else:
            inbound = (
                InstagramMessage.objects.filter(
                    organization=organization,
                    account__organization=organization,
                    conversation__organization=organization,
                    conversation__account__organization=organization,
                    conversation__lead_id=OuterRef("pk"),
                    direction=InstagramMessage.Direction.INBOUND,
                    created_at__gte=start_at,
                    created_at__lt=end_at,
                )
                .filter(
                    account_id=F(
                        "conversation__account_id"
                    )
                )
                .order_by("created_at", "id")
                .values("created_at")[:1]
            )
            cohort = created.annotate(
                _first_inbound_at=Subquery(inbound)
            )
            outbound = (
                InstagramMessage.objects.filter(
                    organization=organization,
                    account__organization=organization,
                    conversation__organization=organization,
                    conversation__account__organization=organization,
                    conversation__lead_id=OuterRef("pk"),
                    direction=InstagramMessage.Direction.OUTBOUND,
                    created_at__gte=OuterRef(
                        "_first_inbound_at"
                    ),
                    created_at__lt=end_at,
                )
                .filter(
                    account_id=F(
                        "conversation__account_id"
                    )
                )
                .order_by("created_at", "id")
                .values("created_at")[:1]
            )

        cohort = cohort.annotate(
            _first_outbound_at=Subquery(outbound)
        )
        inbound_leads = cohort.filter(
            _first_inbound_at__isnull=False
        ).count()
        responded = (
            cohort.filter(
                _first_inbound_at__isnull=False,
                _first_outbound_at__isnull=False,
            )
            .annotate(
                _first_response_duration=ExpressionWrapper(
                    F("_first_outbound_at")
                    - F("_first_inbound_at"),
                    output_field=DurationField(),
                )
            )
        )
        response_aggregate = responded.aggregate(
            responded_leads=Count("id"),
            average_response=Avg(
                "_first_response_duration"
            ),
        )
        responded_leads = int(
            response_aggregate.get(
                "responded_leads"
            )
            or 0
        )
        average_response = response_aggregate.get(
            "average_response"
        )
        average_seconds = (
            round(
                average_response.total_seconds(),
                2,
            )
            if average_response is not None
            else None
        )
        return {
            "inbound_leads": inbound_leads,
            "responded_leads": responded_leads,
            "response_rate": (
                round(
                    responded_leads / inbound_leads,
                    4,
                )
                if inbound_leads
                else None
            ),
            "average_first_response_seconds": (
                average_seconds
            ),
            "method": (
                "First persisted outbound message after the first persisted "
                "inbound message for the same lead, channel and comparison "
                "window."
            ),
        }

    def window(start_at, end_at):
        created = _tenant_safe_leads(
            organization
        ).filter(
            created_at__gte=start_at,
            created_at__lt=end_at,
        )
        lead_count = created.count()

        source_group_count = (
            created.values("lead_source")
            .distinct()
            .count()
        )
        pipeline_group_count = (
            created.values("pipeline_id")
            .distinct()
            .count()
        )
        stage_group_count = (
            created.values("pipeline_id", "stage_id")
            .distinct()
            .count()
        )
        source_rows = list(
            created.values("lead_source")
            .annotate(count=Count("id"))
            .order_by("-count", "lead_source")[
                :CONVERSION_BREAKDOWN_LIMIT
            ]
        )
        pipeline_rows = list(
            created.values("pipeline_id", "pipeline__name")
            .annotate(count=Count("id"))
            .order_by("-count", "pipeline__name")[
                :CONVERSION_BREAKDOWN_LIMIT
            ]
        )
        stage_rows = list(
            created.values(
                "pipeline__name",
                "stage_id",
                "stage__name",
            )
            .annotate(count=Count("id"))
            .order_by("-count", "pipeline__name", "stage__name")[
                :CONVERSION_BREAKDOWN_LIMIT
            ]
        )

        qualified_activity = (
            LeadActivity.objects.filter(
                organization=organization,
                lead_id__in=created.values("id"),
                topic__in=[
                    LeadActivity.Topic.STAGE_CHANGED,
                    LeadActivity.Topic.PIPELINE_CHANGED,
                ],
                created_at__gte=start_at,
                created_at__lt=end_at,
                new_stage_name__iexact="Qualified",
            )
            .filter(
                Q(new_stage__isnull=True)
                | Q(
                    new_stage__pipeline__organization=organization
                )
            )
            .filter(
                Q(new_pipeline__isnull=True)
                | Q(new_pipeline__organization=organization)
            )
            .filter(
                Q(new_stage__isnull=True)
                | Q(new_pipeline__isnull=True)
                | Q(
                    new_stage__pipeline_id=F(
                        "new_pipeline_id"
                    )
                )
            )
        )
        qualified_ids = qualified_activity.values_list(
            "lead_id",
            flat=True,
        ).distinct()
        qualified_count = qualified_ids.count()
        qualified_by_source = {
            row["lead__lead_source"]: row["count"]
            for row in (
                qualified_activity.values("lead__lead_source")
                .annotate(
                    count=Count(
                        "lead_id",
                        distinct=True,
                    )
                )
            )
        }
        sources = [
            {
                "source": row["lead_source"],
                "lead_count": row["count"],
                "lead_share": (
                    round(row["count"] / lead_count, 4)
                    if lead_count
                    else None
                ),
                "qualified_transitions": qualified_by_source.get(
                    row["lead_source"],
                    0,
                ),
                "qualified_transition_rate": (
                    round(
                        qualified_by_source.get(
                            row["lead_source"],
                            0,
                        )
                        / row["count"],
                        4,
                    )
                    if row["count"]
                    else None
                ),
            }
            for row in source_rows
        ]

        qualification_completed_count = (
            created.filter(
                **{
                    "attributes___shvya_ai_qualification__qualification_status": (
                        "completed"
                    )
                }
            ).count()
        )

        returned_pipeline_ids = [
            row["pipeline_id"]
            for row in pipeline_rows
        ]
        qualified_by_pipeline = {
            row["lead__pipeline_id"]: row["count"]
            for row in (
                qualified_activity.filter(
                    lead__pipeline_id__in=(
                        returned_pipeline_ids
                    )
                )
                .values("lead__pipeline_id")
                .annotate(
                    count=Count(
                        "lead_id",
                        distinct=True,
                    )
                )
            )
        }

        stage_reach_qs = (
            LeadActivity.objects.filter(
                organization=organization,
                lead_id__in=created.values("id"),
                topic__in=[
                    LeadActivity.Topic.STAGE_CHANGED,
                    LeadActivity.Topic.PIPELINE_CHANGED,
                ],
                created_at__gte=start_at,
                created_at__lt=end_at,
                new_pipeline__organization=organization,
                new_stage__pipeline__organization=organization,
                new_stage__pipeline_id=F(
                    "new_pipeline_id"
                ),
            )
            .values(
                "new_pipeline_id",
                "new_pipeline_name",
                "new_stage_id",
                "new_stage_name",
            )
            .annotate(
                lead_count=Count(
                    "lead_id",
                    distinct=True,
                )
            )
            .order_by(
                "-lead_count",
                "new_pipeline_name",
                "new_stage_name",
            )
        )
        stage_reach_group_count = (
            stage_reach_qs.count()
        )
        stage_reach_rows = list(
            stage_reach_qs[
                :CONVERSION_BREAKDOWN_LIMIT
            ]
        )

        messages = (
            WhatsAppMessage.objects.filter(
                organization=organization,
                account__organization=organization,
                created_at__gte=start_at,
                created_at__lt=end_at,
            )
            .filter(
                Q(lead__isnull=True)
                | Q(lead_id__in=tenant_lead_ids)
            )
        )
        outbound = messages.filter(
            direction=WhatsAppMessage.Direction.OUTBOUND,
        )
        outbound_status = {
            row["status"]: row["count"]
            for row in outbound.values("status").annotate(
                count=Count("id")
            )
        }
        successful_outbound = sum(
            outbound_status.get(status, 0)
            for status in (
                WhatsAppMessage.Status.SENT,
                WhatsAppMessage.Status.DELIVERED,
                WhatsAppMessage.Status.READ,
            )
        )
        failed_outbound = outbound_status.get(
            WhatsAppMessage.Status.FAILED,
            0,
        )
        outbound_total = outbound.count()

        first_response = {
            "whatsapp": first_response_metrics(
                created=created,
                channel="whatsapp",
                start_at=start_at,
                end_at=end_at,
            ),
            "instagram": first_response_metrics(
                created=created,
                channel="instagram",
                start_at=start_at,
                end_at=end_at,
            ),
        }

        followups = FollowupExecution.objects.filter(
            organization=organization,
            lead_id__in=tenant_lead_ids,
            state__organization=organization,
            state__lead_id=F("lead_id"),
            state__sequence__organization=organization,
            sequence__organization=organization,
            state__sequence_id=F("sequence_id"),
            step__sequence_id=F("sequence_id"),
            scheduled_for__gte=start_at,
            scheduled_for__lt=end_at,
        )
        followup_status = {
            row["status"]: row["count"]
            for row in followups.values("status").annotate(
                count=Count("id")
            )
        }
        followup_total = followups.count()
        cadence_completed = LeadSequenceState.objects.filter(
            organization=organization,
            lead_id__in=tenant_lead_ids,
            sequence__organization=organization,
            completed_at__gte=start_at,
            completed_at__lt=end_at,
        ).count()
        cadence_assignments = LeadSequenceState.objects.filter(
            organization=organization,
            lead_id__in=tenant_lead_ids,
            sequence__organization=organization,
            assigned_at__gte=start_at,
            assigned_at__lt=end_at,
        )
        cadence_assigned = cadence_assignments.count()
        cadence_completed_from_assignments = (
            cadence_assignments.filter(
                completed_at__isnull=False,
                completed_at__lt=end_at,
            ).count()
        )

        workflow_failures = (
            TriggerRun.objects.filter(
                rule__organization=organization,
                lead_id__in=tenant_lead_ids,
                event__organization=organization,
                event__lead_id=F("lead_id"),
                status__in=["failed", "error"],
                created_at__gte=start_at,
                created_at__lt=end_at,
            )
            .filter(
                Q(message__isnull=True)
                | (
                    Q(
                        message__organization=organization,
                        message__account__organization=organization,
                    )
                    & (
                        Q(message__lead__isnull=True)
                        | Q(message__lead_id=F("lead_id"))
                    )
                )
            )
            .count()
        )
        ai_failures = (
            HostedAutomationJob.objects.filter(
                organization=organization,
                account__organization=organization,
                lead_id__in=tenant_lead_ids,
                source_message__organization=organization,
                source_message__account__organization=organization,
                status=HostedAutomationJob.Status.FAILED,
                created_at__gte=start_at,
                created_at__lt=end_at,
            )
            .filter(
                account_id=F("source_message__account_id"),
            )
            .filter(
                Q(source_message__lead__isnull=True)
                | Q(source_message__lead_id=F("lead_id"))
            )
            .count()
        )

        campaign_deliveries = (
            CampaignDelivery.objects.filter(
                campaign__organization=organization,
                campaign__account__organization=organization,
                campaign__pipeline__organization=organization,
                created_at__gte=start_at,
                created_at__lt=end_at,
            )
            .filter(
                Q(campaign__stage__isnull=True)
                | Q(
                    campaign__stage__pipeline_id=F(
                        "campaign__pipeline_id"
                    )
                )
            )
            .filter(
                Q(lead__isnull=True)
                | Q(lead_id__in=tenant_lead_ids)
            )
        )
        campaign_states = {
            row["state"]: row["count"]
            for row in campaign_deliveries.values(
                "state"
            ).annotate(
                count=Count("id")
            )
        }
        campaign_total = campaign_deliveries.count()
        campaign_delivered = campaign_deliveries.filter(
            delivered_at__isnull=False,
        ).count()
        campaign_replied = campaign_deliveries.filter(
            replied_at__isnull=False,
        ).count()

        lost_transition_count = 0
        lost_reason_rows = []
        lost_reason_total = 0
        if configured_lost_stage is not None:
            lost_activity = LeadActivity.objects.filter(
                organization=organization,
                lead_id__in=created.values("id"),
                topic__in=[
                    LeadActivity.Topic.STAGE_CHANGED,
                    LeadActivity.Topic.PIPELINE_CHANGED,
                ],
                created_at__gte=start_at,
                created_at__lt=end_at,
                new_stage_id=configured_lost_stage.id,
            )
            lost_ids = lost_activity.values(
                "lead_id"
            ).distinct()
            lost_transition_count = lost_ids.count()
            if (
                lost_reason_definition is not None
                and lost_transition_count
            ):
                lost_reason_qs = (
                    _tenant_safe_leads(
                        organization
                    )
                    .filter(id__in=lost_ids)
                    .annotate(
                        _loss_reason=KeyTextTransform(
                            lost_reason_definition.key,
                            "attributes",
                        )
                    )
                    .exclude(
                        _loss_reason__isnull=True
                    )
                    .exclude(_loss_reason="")
                    .values("_loss_reason")
                    .annotate(count=Count("id"))
                    .order_by(
                        "-count",
                        "_loss_reason",
                    )
                )
                lost_reason_total = (
                    lost_reason_qs.count()
                )
                lost_reason_rows = list(
                    lost_reason_qs[
                        :LOST_REASON_BREAKDOWN_LIMIT
                    ]
                )

        return {
            "lead_volume": lead_count,
            "qualified_transitions": qualified_count,
            "qualified_transition_rate": (
                round(qualified_count / lead_count, 4)
                if lead_count
                else None
            ),
            "qualification_completion": {
                "completed_leads": (
                    qualification_completed_count
                ),
                "completion_rate": (
                    round(
                        qualification_completed_count
                        / lead_count,
                        4,
                    )
                    if lead_count
                    else None
                ),
                "method": (
                    "Current persisted SHVYA qualification status for leads "
                    "created in this comparison cohort."
                ),
            },
            "source_mix": sources,
            "pipeline_mix": [
                {
                    "pipeline_id": str(
                        row["pipeline_id"]
                    ),
                    "pipeline": row["pipeline__name"],
                    "lead_count": row["count"],
                    "qualified_transitions": (
                        qualified_by_pipeline.get(
                            row["pipeline_id"],
                            0,
                        )
                    ),
                    "qualified_transition_rate": (
                        round(
                            qualified_by_pipeline.get(
                                row["pipeline_id"],
                                0,
                            )
                            / row["count"],
                            4,
                        )
                        if row["count"]
                        else None
                    ),
                }
                for row in pipeline_rows
            ],
            "stage_mix": [
                {
                    "pipeline": row["pipeline__name"],
                    "stage_id": str(
                        row["stage_id"]
                    ),
                    "stage": row["stage__name"],
                    "lead_count": row["count"],
                }
                for row in stage_rows
            ],
            "stage_conversion_proxy": {
                "method": (
                    "Distinct leads from the created cohort that reached each "
                    "persisted tenant-owned stage through a stage/pipeline "
                    "transition during the comparison window."
                ),
                "stages": [
                    {
                        "pipeline_id": str(
                            row["new_pipeline_id"]
                        ),
                        "pipeline": (
                            row["new_pipeline_name"]
                        ),
                        "stage_id": str(
                            row["new_stage_id"]
                        ),
                        "stage": (
                            row["new_stage_name"]
                        ),
                        "lead_count": row["lead_count"],
                        "reach_rate": (
                            round(
                                row["lead_count"]
                                / lead_count,
                                4,
                            )
                            if lead_count
                            else None
                        ),
                    }
                    for row in stage_reach_rows
                ],
                "stage_group_count": (
                    stage_reach_group_count
                ),
                "stages_returned": len(
                    stage_reach_rows
                ),
                "stages_truncated": (
                    stage_reach_group_count
                    > len(stage_reach_rows)
                ),
            },
            "breakdown_counts": {
                "source_groups": source_group_count,
                "source_groups_returned": len(
                    source_rows
                ),
                "source_groups_truncated": (
                    source_group_count
                    > len(source_rows)
                ),
                "pipeline_groups": pipeline_group_count,
                "pipeline_groups_returned": len(
                    pipeline_rows
                ),
                "pipeline_groups_truncated": (
                    pipeline_group_count
                    > len(pipeline_rows)
                ),
                "stage_groups": stage_group_count,
                "stage_groups_returned": len(
                    stage_rows
                ),
                "stage_groups_truncated": (
                    stage_group_count
                    > len(stage_rows)
                ),
            },
            "messaging": {
                "outbound_total": outbound_total,
                "successful_outbound": successful_outbound,
                "failed_outbound": failed_outbound,
                "success_rate": (
                    round(
                        successful_outbound
                        / outbound_total,
                        4,
                    )
                    if outbound_total
                    else None
                ),
                "status_counts": outbound_status,
                "first_response": first_response,
            },
            "followups": {
                "scheduled_executions": followup_total,
                "status_counts": followup_status,
                "failed": followup_status.get(
                    FollowupExecution.Status.FAILED,
                    0,
                ),
                "blocked": followup_status.get(
                    FollowupExecution.Status.BLOCKED,
                    0,
                ),
                "failure_or_block_rate": (
                    round(
                        (
                            followup_status.get(
                                FollowupExecution.Status.FAILED,
                                0,
                            )
                            + followup_status.get(
                                FollowupExecution.Status.BLOCKED,
                                0,
                            )
                        )
                        / followup_total,
                        4,
                    )
                    if followup_total
                    else None
                ),
                "cadences_completed": cadence_completed,
                "cadences_assigned": cadence_assigned,
                "assigned_cadences_completed_by_period_end": (
                    cadence_completed_from_assignments
                ),
                "assigned_cadence_completion_rate": (
                    round(
                        cadence_completed_from_assignments
                        / cadence_assigned,
                        4,
                    )
                    if cadence_assigned
                    else None
                ),
            },
            "failures": {
                "workflow_failures": workflow_failures,
                "hosted_ai_failures": ai_failures,
            },
            "campaigns": {
                "deliveries": campaign_total,
                "delivered": campaign_delivered,
                "replied": campaign_replied,
                "delivery_rate": (
                    round(
                        campaign_delivered
                        / campaign_total,
                        4,
                    )
                    if campaign_total
                    else None
                ),
                "reply_rate": (
                    round(
                        campaign_replied
                        / campaign_total,
                        4,
                    )
                    if campaign_total
                    else None
                ),
                "state_counts": campaign_states,
            },
            "lost": {
                "lost_stage_configured": (
                    configured_lost_stage
                    is not None
                ),
                "lost_stage_id": (
                    str(configured_lost_stage.id)
                    if configured_lost_stage
                    else None
                ),
                "lost_stage": (
                    configured_lost_stage.name
                    if configured_lost_stage
                    else None
                ),
                "lost_transitions": (
                    lost_transition_count
                ),
                "reason_capture_configured": (
                    lost_reason_definition
                    is not None
                ),
                "reason_attribute_key": (
                    lost_reason_definition.key
                    if lost_reason_definition
                    else None
                ),
                "reason_counts": [
                    {
                        "reason": sanitize_text(
                            row["_loss_reason"],
                            limit=200,
                        ),
                        "count": row["count"],
                    }
                    for row in lost_reason_rows
                ],
                "reason_group_count": (
                    lost_reason_total
                ),
                "reasons_returned": len(
                    lost_reason_rows
                ),
                "reasons_truncated": (
                    lost_reason_total
                    > len(lost_reason_rows)
                ),
            },
        }

    current = window(current_start, now)
    previous = window(
        previous_start,
        current_start,
    )

    observations = []
    comparable_metrics = (
        (
            "lead_volume",
            current["lead_volume"],
            previous["lead_volume"],
        ),
        (
            "qualified_transition_rate",
            current["qualified_transition_rate"],
            previous["qualified_transition_rate"],
        ),
        (
            "qualification_completion_rate",
            current["qualification_completion"][
                "completion_rate"
            ],
            previous["qualification_completion"][
                "completion_rate"
            ],
        ),
        (
            "message_success_rate",
            current["messaging"]["success_rate"],
            previous["messaging"]["success_rate"],
        ),
        (
            "whatsapp_first_response_seconds",
            current["messaging"]["first_response"][
                "whatsapp"
            ]["average_first_response_seconds"],
            previous["messaging"]["first_response"][
                "whatsapp"
            ]["average_first_response_seconds"],
        ),
        (
            "instagram_first_response_seconds",
            current["messaging"]["first_response"][
                "instagram"
            ]["average_first_response_seconds"],
            previous["messaging"]["first_response"][
                "instagram"
            ]["average_first_response_seconds"],
        ),
        (
            "lost_transitions",
            current["lost"]["lost_transitions"],
            previous["lost"]["lost_transitions"],
        ),
        (
            "followup_failure_or_block_rate",
            current["followups"][
                "failure_or_block_rate"
            ],
            previous["followups"][
                "failure_or_block_rate"
            ],
        ),
        (
            "assigned_cadence_completion_rate",
            current["followups"][
                "assigned_cadence_completion_rate"
            ],
            previous["followups"][
                "assigned_cadence_completion_rate"
            ],
        ),
        (
            "workflow_failures",
            current["failures"][
                "workflow_failures"
            ],
            previous["failures"][
                "workflow_failures"
            ],
        ),
        (
            "hosted_ai_failures",
            current["failures"][
                "hosted_ai_failures"
            ],
            previous["failures"][
                "hosted_ai_failures"
            ],
        ),
        (
            "campaign_delivery_rate",
            current["campaigns"]["delivery_rate"],
            previous["campaigns"]["delivery_rate"],
        ),
        (
            "campaign_reply_rate",
            current["campaigns"]["reply_rate"],
            previous["campaigns"]["reply_rate"],
        ),
    )
    for (
        metric,
        current_value,
        previous_value,
    ) in comparable_metrics:
        if current_value != previous_value:
            observations.append(
                {
                    "classification": "Measured",
                    "metric": metric,
                    "current": current_value,
                    "previous": previous_value,
                }
            )

    if (
        current["failures"]["workflow_failures"]
        > previous["failures"]["workflow_failures"]
    ):
        observations.append(
            {
                "classification": "Likely contributor",
                "metric": "workflow_failures",
                "current": current["failures"][
                    "workflow_failures"
                ],
                "previous": previous["failures"][
                    "workflow_failures"
                ],
                "note": (
                    "Workflow failures increased during the same comparison "
                    "window. This is correlation, not proof of conversion "
                    "causality."
                ),
            }
        )
    if (
        current["messaging"]["success_rate"]
        is not None
        and previous["messaging"]["success_rate"]
        is not None
        and current["messaging"]["success_rate"]
        < previous["messaging"]["success_rate"]
    ):
        observations.append(
            {
                "classification": "Likely contributor",
                "metric": "message_delivery",
                "current": current["messaging"][
                    "success_rate"
                ],
                "previous": previous["messaging"][
                    "success_rate"
                ],
                "note": (
                    "Outbound WhatsApp success rate decreased while the compared "
                    "conversion proxy was measured. Causality requires lead-level "
                    "evidence."
                ),
            }
        )

    current_sources = {
        row["source"]: row
        for row in current["source_mix"]
    }
    previous_sources = {
        row["source"]: row
        for row in previous["source_mix"]
    }
    material_source_shift = False
    for source in sorted(
        set(current_sources)
        | set(previous_sources)
    ):
        current_row = (
            current_sources.get(source) or {}
        )
        previous_row = (
            previous_sources.get(source) or {}
        )
        current_share = (
            current_row.get("lead_share") or 0
        )
        previous_share = (
            previous_row.get("lead_share") or 0
        )
        if (
            abs(
                current_share
                - previous_share
            )
            >= 0.05
        ):
            material_source_shift = True
            observations.append(
                {
                    "classification": "Measured",
                    "metric": "source_mix_share",
                    "source": source,
                    "current": current_share,
                    "previous": previous_share,
                    "note": (
                        "Lead-source share changed by at least "
                        "5 percentage points."
                    ),
                }
            )

        current_rate = current_row.get(
            "qualified_transition_rate"
        )
        previous_rate = previous_row.get(
            "qualified_transition_rate"
        )
        if (
            current_rate is not None
            and previous_rate is not None
            and current_row.get(
                "lead_count",
                0,
            )
            >= 5
            and previous_row.get(
                "lead_count",
                0,
            )
            >= 5
            and current_rate != previous_rate
        ):
            observations.append(
                {
                    "classification": "Measured",
                    "metric": (
                        "source_qualified_transition_rate"
                    ),
                    "source": source,
                    "current": current_rate,
                    "previous": previous_rate,
                }
            )

    if material_source_shift:
        observations.append(
            {
                "classification": "Hypothesis",
                "metric": "source_mix",
                "note": (
                    "Lead-source mix changed materially. Compare source-specific "
                    "qualification rates before attributing any overall conversion "
                    "change to lead quality or marketing source."
                ),
            }
        )

    current_followup_rate = (
        current["followups"][
            "failure_or_block_rate"
        ]
    )
    previous_followup_rate = (
        previous["followups"][
            "failure_or_block_rate"
        ]
    )
    if (
        current_followup_rate is not None
        and previous_followup_rate is not None
        and current_followup_rate
        > previous_followup_rate
    ):
        observations.append(
            {
                "classification": "Likely contributor",
                "metric": (
                    "followup_failure_or_block_rate"
                ),
                "current": current_followup_rate,
                "previous": previous_followup_rate,
                "note": (
                    "Failed/blocked follow-up execution increased in the same "
                    "comparison window. Confirm impact on affected leads before "
                    "claiming conversion causality."
                ),
            }
        )

    if (
        current["failures"]["hosted_ai_failures"]
        > previous["failures"]["hosted_ai_failures"]
    ):
        observations.append(
            {
                "classification": "Likely contributor",
                "metric": "hosted_ai_failures",
                "current": current["failures"][
                    "hosted_ai_failures"
                ],
                "previous": previous["failures"][
                    "hosted_ai_failures"
                ],
                "note": (
                    "Hosted AI execution failures increased. Inspect affected "
                    "lead/message traces to establish whether they interrupted "
                    "engagement."
                ),
            }
        )

    for metric, current_rate, previous_rate in (
        (
            "campaign_delivery_rate",
            current["campaigns"][
                "delivery_rate"
            ],
            previous["campaigns"][
                "delivery_rate"
            ],
        ),
        (
            "campaign_reply_rate",
            current["campaigns"]["reply_rate"],
            previous["campaigns"]["reply_rate"],
        ),
    ):
        if (
            current_rate is not None
            and previous_rate is not None
            and current_rate < previous_rate
        ):
            observations.append(
                {
                    "classification": "Likely contributor",
                    "metric": metric,
                    "current": current_rate,
                    "previous": previous_rate,
                    "note": (
                        "Campaign performance deteriorated in the same period. "
                        "This is correlation until matched to affected leads and "
                        "downstream stage outcomes."
                    ),
                }
            )

    ageing_leads = _tenant_safe_leads(
        organization
    )
    terminal_stage_ids = [
        stage_id
        for stage_id in (
            (
                configured_won_stage.id
                if configured_won_stage
                else None
            ),
            (
                configured_lost_stage.id
                if configured_lost_stage
                else None
            ),
        )
        if stage_id is not None
    ]
    if terminal_stage_ids:
        ageing_leads = ageing_leads.exclude(
            stage_id__in=terminal_stage_ids
        )

    one_day_ago = now - timedelta(days=1)
    three_days_ago = now - timedelta(days=3)
    seven_days_ago = now - timedelta(days=7)
    fourteen_days_ago = now - timedelta(days=14)
    thirty_days_ago = now - timedelta(days=30)
    stall_cutoff = now - timedelta(
        days=stall_day_threshold
    )
    ageing = ageing_leads.aggregate(
        active_lead_count=Count("id"),
        stalled_count=Count(
            "id",
            filter=Q(
                stage_entered_at__lte=stall_cutoff
            ),
        ),
        under_1_day=Count(
            "id",
            filter=Q(
                stage_entered_at__gt=one_day_ago
            ),
        ),
        days_1_to_3=Count(
            "id",
            filter=Q(
                stage_entered_at__lte=one_day_ago,
                stage_entered_at__gt=three_days_ago,
            ),
        ),
        days_3_to_7=Count(
            "id",
            filter=Q(
                stage_entered_at__lte=three_days_ago,
                stage_entered_at__gt=seven_days_ago,
            ),
        ),
        days_7_to_14=Count(
            "id",
            filter=Q(
                stage_entered_at__lte=seven_days_ago,
                stage_entered_at__gt=fourteen_days_ago,
            ),
        ),
        days_14_to_30=Count(
            "id",
            filter=Q(
                stage_entered_at__lte=fourteen_days_ago,
                stage_entered_at__gt=thirty_days_ago,
            ),
        ),
        days_30_plus=Count(
            "id",
            filter=Q(
                stage_entered_at__lte=thirty_days_ago
            ),
        ),
        oldest_stage_entered_at=Min(
            "stage_entered_at"
        ),
    )
    active_lead_count = int(
        ageing.get("active_lead_count") or 0
    )
    stalled_count = int(
        ageing.get("stalled_count") or 0
    )
    oldest_stage_entered_at = ageing.get(
        "oldest_stage_entered_at"
    )
    oldest_stage_age_days = (
        round(
            (
                now
                - oldest_stage_entered_at
            ).total_seconds()
            / 86400,
            2,
        )
        if oldest_stage_entered_at
        else None
    )
    fixed_seven_day_stale = (
        _tenant_safe_leads(
            organization
        )
        .filter(
            stage_entered_at__lt=(
                now - timedelta(days=7)
            )
        )
        .count()
    )

    return ToolExecution(
        data={
            "comparison": {
                "days_per_period": days,
                "current_period": {
                    "start": (
                        current_start.isoformat()
                    ),
                    "end": now.isoformat(),
                    **current,
                },
                "previous_period": {
                    "start": (
                        previous_start.isoformat()
                    ),
                    "end": (
                        current_start.isoformat()
                    ),
                    **previous,
                },
            },
            "current_snapshot": {
                "leads_in_current_stage_for_7_plus_days": (
                    fixed_seven_day_stale
                ),
                "lead_ageing": {
                    "scope": "current_non_terminal_stage_snapshot",
                    "stall_day_threshold": (
                        stall_day_threshold
                    ),
                    "threshold_source": (
                        "analytics_settings"
                        if analytics_settings
                        else "default"
                    ),
                    "active_lead_count": (
                        active_lead_count
                    ),
                    "stalled_count": stalled_count,
                    "stalled_rate": (
                        round(
                            stalled_count
                            / active_lead_count,
                            4,
                        )
                        if active_lead_count
                        else None
                    ),
                    "oldest_stage_age_days": (
                        oldest_stage_age_days
                    ),
                    "buckets": {
                        "under_1_day": (
                            ageing["under_1_day"]
                        ),
                        "days_1_to_3": (
                            ageing["days_1_to_3"]
                        ),
                        "days_3_to_7": (
                            ageing["days_3_to_7"]
                        ),
                        "days_7_to_14": (
                            ageing["days_7_to_14"]
                        ),
                        "days_14_to_30": (
                            ageing["days_14_to_30"]
                        ),
                        "days_30_plus": (
                            ageing["days_30_plus"]
                        ),
                    },
                    "excluded_terminal_stage_ids": [
                        str(item)
                        for item in terminal_stage_ids
                    ],
                },
            },
            "observations": observations,
            "limitations": [
                (
                    "Qualified transition rate is a CRM transition proxy: it uses "
                    "persisted activity and lead volume, not a claim that every "
                    "Qualified lead converted to revenue."
                ),
                (
                    "Pipeline/stage mix describes the current CRM location of leads "
                    "created in each comparison period; it is not a historical "
                    "stage snapshot. Pipeline Qualified rates therefore use each "
                    "lead's current pipeline as the cohort grouping."
                ),
                (
                    "Qualification completion uses each cohort lead's current "
                    "persisted qualification status. It does not reconstruct the "
                    "status as it existed at the historical period end."
                ),
                (
                    "Stage conversion is reported as a persisted stage-reach proxy "
                    "from CRM transition activity, not as revenue conversion."
                ),
                (
                    "First-response metrics measure the first persisted outbound "
                    "after the first persisted inbound for the same lead/channel "
                    "inside each period. They do not infer whether the responder "
                    "was AI or a human."
                ),
                (
                    "Lead ageing is a current stage-age snapshot. Historical stage "
                    "age is not reconstructed for the previous comparison period."
                ),
                (
                    "Lost reasons are reported only when Analytics has an "
                    "organization-owned lost stage and CRM has a non-sensitive "
                    "Lost Reason/Loss Reason attribute. Missing reason capture is "
                    "reported as unavailable rather than guessed."
                ),
                (
                    "Likely-contributor labels are correlation, not proven "
                    "causality."
                ),
            ],
        },
        capability=CAP_DIAGNOSTICS_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={
            "period_days": days,
            "observation_count": len(
                observations
            ),
            "first_response_metrics": True,
            "lead_ageing_snapshot": True,
            "lost_reason_analysis": True,
        },
    )
