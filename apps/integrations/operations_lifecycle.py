"""Archive-first lifecycle operations for SHVYA Operations MCP.

Permanent deletion is intentionally narrow. Archive operations retain history,
and every lifecycle dry-run reports dependencies, bounded affected-record
counts, protected-object state, migration requirements, reversibility, and
whether execution can proceed.
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from apps.channels.models import WhatsAppAccount
from apps.crm.models import AttributeDefinition, Lead, Pipeline, Stage
from apps.followups.models import FollowupSequence, LeadSequenceState
from apps.integrations.models import GoogleSheetIntegration, MetaLeadForm
from apps.integrations.operations_models import OperationsAuditEvent
from apps.integrations.operations_policy import (
    CAP_ATTRIBUTE_CONFIG_WRITE,
    CAP_CADENCE_CONFIG_WRITE,
    CAP_PIPELINE_CONFIG_WRITE,
    CAP_STAGE_CONFIG_WRITE,
    CAP_WORKFLOW_CONFIG_WRITE,
    approval_required,
)
from apps.triggers.models import SmartTrigger, TriggerRun
from services.channels.hosted_whatsapp_service import get_pipeline_for_account
from services.crm.attribute_service import delete_attribute_definition

from apps.integrations.operations_tools import (
    OperationsManualFixRequired,
    OperationsPermissionError,
    OperationsToolError,
    ToolExecution,
    _ensure_approved_proposal_unchanged,
    _organization_for,
    _proposal_digest,
    _uuid,
    _write_gate,
)


ATTRIBUTE_PURGE_LIMIT = 5_000
ACTIVE_RUN_STATUSES = {
    "pending",
    "scheduled",
    "queued",
    "dispatching",
    "email_ready",
    "sending",
}


def _reverse_relation_counts(obj):
    rows = []
    for relation in obj._meta.get_fields(include_hidden=True):
        if not getattr(relation, "auto_created", False):
            continue
        if getattr(relation, "concrete", False):
            continue
        field = getattr(relation, "field", None)
        related_model = getattr(relation, "related_model", None)
        if field is None or related_model is None:
            continue
        if getattr(field, "many_to_many", False):
            continue
        try:
            count = related_model._base_manager.filter(
                **{field.name: obj}
            ).count()
        except (TypeError, ValueError):
            continue
        if not count:
            continue
        on_delete = getattr(
            getattr(field, "remote_field", None),
            "on_delete",
            None,
        )
        rows.append(
            {
                "model": related_model._meta.label,
                "field": field.name,
                "count": int(count),
                "on_delete": getattr(on_delete, "__name__", str(on_delete or "")),
            }
        )
    return sorted(rows, key=lambda item: (item["model"], item["field"]))


def _workflow_refs(*, organization, pipeline_id=None, stage_id=None, cadence_id=None, attribute_key=None):
    rows = []
    for rule in SmartTrigger.objects.filter(
        organization=organization,
        is_active=True,
    ).order_by("position", "id")[:500]:
        conditions = rule.conditions if isinstance(rule.conditions, dict) else {}
        action = rule.action if isinstance(rule.action, dict) else {}
        reasons = []

        if pipeline_id or stage_id:
            for scope in conditions.get("scopes") or []:
                if not isinstance(scope, dict):
                    continue
                if pipeline_id and str(scope.get("pipeline") or "") == str(pipeline_id):
                    reasons.append("trigger_scope_pipeline")
                if stage_id and str(stage_id) in {
                    str(item) for item in (scope.get("stages") or [])
                }:
                    reasons.append("trigger_scope_stage")
            if rule.action_type == "move_stage":
                if pipeline_id and str(action.get("pipeline") or "") == str(pipeline_id):
                    reasons.append("action_pipeline")
                if stage_id and str(action.get("stage") or "") == str(stage_id):
                    reasons.append("action_stage")

        if cadence_id:
            if str(cadence_id) in {
                str(item) for item in (conditions.get("sequences") or [])
            }:
                reasons.append("trigger_sequence")
            if (
                rule.action_type == "start_sequence"
                and str(action.get("sequence") or "") == str(cadence_id)
            ):
                reasons.append("action_sequence")

        if attribute_key:
            for item in conditions.get("attributes") or []:
                if isinstance(item, dict) and str(item.get("key") or "") == attribute_key:
                    reasons.append("condition_attribute")
            if (
                rule.action_type == "attribute"
                and str(action.get("key") or "") == attribute_key
            ):
                reasons.append("action_attribute")
            if str(action.get("date_attribute") or "") == attribute_key:
                reasons.append("date_attribute")

        if reasons:
            rows.append(
                {
                    "workflow_id": str(rule.id),
                    "workflow_name": rule.name,
                    "enabled": rule.enabled,
                    "references": sorted(set(reasons)),
                }
            )
    return rows


def _qualification_refs(*, organization, stage_id=None, attribute_key=None):
    from apps.integrations.operations_extended_tools import _qualification_public_snapshot

    snapshot = _qualification_public_snapshot(organization)
    refs = []
    completion = snapshot.get("completion_stage")
    if stage_id and completion and str(completion.get("id")) == str(stage_id):
        refs.append(
            {
                "type": "qualification_completion_stage",
                "stage_id": str(stage_id),
            }
        )
    if attribute_key:
        requirement_ids = [
            requirement_id
            for requirement_id, keys in (snapshot.get("mappings") or {}).items()
            if attribute_key in {str(key) for key in keys}
        ]
        if requirement_ids:
            refs.append(
                {
                    "type": "qualification_attribute_mapping",
                    "attribute_key": attribute_key,
                    "requirement_ids": requirement_ids,
                }
            )
    return refs


def _stage_report(*, organization, stage):
    leads = Lead.objects.filter(
        organization=organization,
        stage=stage,
        pipeline=stage.pipeline,
    ).count()
    workflows = _workflow_refs(
        organization=organization,
        pipeline_id=stage.pipeline_id,
        stage_id=stage.id,
    )
    qualification = _qualification_refs(
        organization=organization,
        stage_id=stage.id,
    )
    relations = _reverse_relation_counts(stage)

    blocking_relations = [
        item
        for item in relations
        if item["model"] == "crm.Lead"
        or item["on_delete"] in {"PROTECT", "RESTRICT"}
        or (
            item["on_delete"] == "CASCADE"
            and item["model"] not in {"crm.LeadActivity"}
        )
    ]
    # Lead is already represented as a first-class count.
    blocking_relations = [
        item for item in blocking_relations if item["model"] != "crm.Lead"
    ]

    protected = bool(stage.is_system_locked)
    migration = []
    if leads:
        migration.append(f"Move {leads} lead(s) to another active stage.")
    if workflows:
        migration.append("Update or archive Workflows that reference this stage.")
    if qualification:
        migration.append("Move qualification completion to another active stage.")
    if blocking_relations:
        migration.append("Repoint or disable integrations/configuration that reference this stage.")

    return {
        "object": {
            "type": "stage",
            "id": str(stage.id),
            "name": stage.name,
            "pipeline_id": str(stage.pipeline_id),
            "active": stage.is_active,
        },
        "protected_object_status": {
            "protected": protected,
            "reason": (
                "SHVYA system stages New leads and Qualified cannot be archived or deleted."
                if protected else ""
            ),
        },
        "dependencies": {
            "workflow_references": workflows,
            "qualification_references": qualification,
            "relational_references": relations,
        },
        "affected_records": {
            "leads": leads,
            "relational_records": sum(item["count"] for item in relations),
        },
        "blocking_dependency_count": (
            len(workflows)
            + len(qualification)
            + len(blocking_relations)
            + (1 if leads else 0)
        ),
        "migration_required": bool(migration),
        "migration_requirements": migration,
        "can_archive": not protected and not migration,
        "can_delete": not protected and not migration,
    }


def _attribute_report(*, organization, attribute):
    lead_values = Lead.objects.filter(
        organization=organization,
        **{"attributes__has_key": attribute.key},
    ).count()
    workflows = _workflow_refs(
        organization=organization,
        attribute_key=attribute.key,
    )
    qualification = _qualification_refs(
        organization=organization,
        attribute_key=attribute.key,
    )
    stage_refs = []
    for stage in Stage.objects.filter(
        pipeline__organization=organization,
        pipeline__is_active=True,
        is_active=True,
    ).select_related("pipeline")[:1000]:
        required = {
            str(item)
            for item in ((stage.config or {}).get("required_attribute_ids") or [])
        }
        if str(attribute.id) in required:
            stage_refs.append(
                {
                    "stage_id": str(stage.id),
                    "stage_name": stage.name,
                    "pipeline_id": str(stage.pipeline_id),
                    "pipeline_name": stage.pipeline.name,
                }
            )

    meta_refs = [
        {
            "form_id": str(form.id),
            "form_name": form.form_name,
        }
        for form in MetaLeadForm.objects.filter(
            page__organization=organization,
            is_active=True,
        ).only("id", "form_name", "field_mapping")
        if f"attribute:{attribute.key}" in (form.field_mapping or {})
    ][:200]

    sheets = []
    for integration in GoogleSheetIntegration.objects.filter(
        organization=organization,
        is_enabled=True,
    ).only("id", "name", "mapping")[:200]:
        values = {
            str(value)
            for value in (integration.mapping or {}).values()
            if value is not None
        }
        if f"attribute:{attribute.id}" in values:
            sheets.append(
                {
                    "integration_id": str(integration.id),
                    "integration_name": integration.name,
                }
            )

    settings = organization.settings if isinstance(organization.settings, dict) else {}
    memory = settings.get("ai_memory") if isinstance(settings.get("ai_memory"), dict) else {}
    memory_mappings = (
        memory.get("field_mappings")
        if isinstance(memory.get("field_mappings"), dict)
        else {}
    )
    memory_refs = [
        str(name)
        for name, key in memory_mappings.items()
        if str(key) == attribute.key
    ]

    migration = []
    if workflows:
        migration.append("Remove or migrate Workflow references to this attribute.")
    if qualification:
        migration.append("Remove or migrate qualification mappings to this attribute.")
    if stage_refs:
        migration.append("Remove this attribute from stage-entry requirements.")
    if meta_refs:
        migration.append("Update active Meta Lead form field mappings.")
    if sheets:
        migration.append("Update active Google Sheets field mappings.")
    if memory_refs:
        migration.append("Update AI memory field mappings in organization settings.")

    return {
        "object": {
            "type": "attribute",
            "id": str(attribute.id),
            "key": attribute.key,
            "name": attribute.name,
            "active": attribute.is_active,
        },
        "protected_object_status": {
            "protected": False,
            "reason": "",
        },
        "dependencies": {
            "workflow_references": workflows,
            "qualification_references": qualification,
            "stage_requirement_references": stage_refs,
            "meta_lead_form_references": meta_refs,
            "google_sheet_references": sheets,
            "ai_memory_references": memory_refs,
        },
        "affected_records": {
            "lead_values": lead_values,
        },
        "blocking_dependency_count": (
            len(workflows)
            + len(qualification)
            + len(stage_refs)
            + len(meta_refs)
            + len(sheets)
            + len(memory_refs)
        ),
        "migration_required": bool(migration),
        "migration_requirements": migration,
        "can_archive": not migration,
        "can_delete_without_value_purge": not migration and lead_values == 0,
    }


def _pipeline_report(*, organization, pipeline):
    leads = Lead.objects.filter(
        organization=organization,
        pipeline=pipeline,
    ).count()
    workflows = _workflow_refs(
        organization=organization,
        pipeline_id=pipeline.id,
    )
    accounts = []
    for account in WhatsAppAccount.objects.filter(
        organization=organization,
        is_active=True,
    ).defer("access_token")[:100]:
        if get_pipeline_for_account(account=account) == pipeline:
            accounts.append(
                {
                    "account_id": str(account.id),
                    "connection_type": account.connection_type,
                    "display_phone_number": account.display_phone_number,
                    "status": account.status,
                }
            )

    relations = _reverse_relation_counts(pipeline)
    ignored_models = {
        "crm.Stage",
        "crm.PipelinePermission",
        "crm.LeadActivity",
    }
    blocking_relations = [
        item
        for item in relations
        if item["model"] not in ignored_models
        and (
            item["on_delete"] in {"PROTECT", "RESTRICT"}
            or item["on_delete"] == "CASCADE"
        )
    ]
    blocking_relations = [
        item for item in blocking_relations if item["model"] != "crm.Lead"
    ]

    migration = []
    if leads:
        migration.append(f"Move {leads} lead(s) to another active pipeline.")
    if workflows:
        migration.append("Update or archive Workflows that reference this pipeline.")
    if accounts:
        migration.append("Rebind connected WhatsApp account routing to another active pipeline.")
    if blocking_relations:
        migration.append("Repoint or disable integrations/configuration that reference this pipeline.")

    return {
        "object": {
            "type": "pipeline",
            "id": str(pipeline.id),
            "name": pipeline.name,
            "active": pipeline.is_active,
        },
        "protected_object_status": {
            "protected": False,
            "reason": "",
        },
        "dependencies": {
            "workflow_references": workflows,
            "whatsapp_routing": accounts,
            "relational_references": relations,
        },
        "affected_records": {
            "leads": leads,
            "stages": pipeline.stages.count(),
            "relational_records": sum(item["count"] for item in relations),
        },
        "blocking_dependency_count": (
            len(workflows)
            + len(accounts)
            + len(blocking_relations)
            + (1 if leads else 0)
        ),
        "migration_required": bool(migration),
        "migration_requirements": migration,
        "can_archive": not migration,
    }


def _cadence_report(*, organization, cadence):
    active_states = LeadSequenceState.objects.filter(
        organization=organization,
        sequence=cadence,
        status__in=[
            LeadSequenceState.Status.ACTIVE,
            LeadSequenceState.Status.PAUSED,
        ],
    ).count()
    workflows = _workflow_refs(
        organization=organization,
        cadence_id=cadence.id,
    )
    migration = []
    if active_states:
        migration.append(
            f"Stop, complete, or move {active_states} active/paused lead sequence state(s)."
        )
    if workflows:
        migration.append("Update or archive Workflows that reference this Cadence.")
    return {
        "object": {
            "type": "cadence",
            "id": str(cadence.id),
            "name": cadence.name,
            "active": cadence.is_active,
        },
        "protected_object_status": {"protected": False, "reason": ""},
        "dependencies": {
            "workflow_references": workflows,
        },
        "affected_records": {
            "active_or_paused_lead_states": active_states,
            "steps": cadence.steps.count(),
            "historical_executions": cadence.executions.count(),
        },
        "blocking_dependency_count": len(workflows) + (1 if active_states else 0),
        "migration_required": bool(migration),
        "migration_requirements": migration,
        "can_archive": not migration,
    }


def _workflow_report(*, organization, workflow):
    pending_runs = TriggerRun.objects.filter(
        rule=workflow,
        status__in=ACTIVE_RUN_STATUSES,
    ).count()
    return {
        "object": {
            "type": "workflow",
            "id": str(workflow.id),
            "name": workflow.name,
            "active": workflow.is_active,
            "enabled": workflow.enabled,
        },
        "protected_object_status": {"protected": False, "reason": ""},
        "dependencies": {},
        "affected_records": {
            "pending_or_queued_runs": pending_runs,
            "historical_runs": workflow.runs.count(),
        },
        "blocking_dependency_count": 0,
        "migration_required": False,
        "migration_requirements": [],
        "can_archive": True,
    }


def _lifecycle_dry_run(
    *,
    identity,
    organization,
    capability,
    target_type,
    target_id,
    reason,
    operation,
    report,
    can_apply,
    reversible,
    proposal,
):
    return ToolExecution(
        data={
            "status": "DRY_RUN",
            "operation": operation,
            **report,
            "can_apply": bool(can_apply),
            "reversible": bool(reversible),
            "approval_required": (
                bool(can_apply)
                and approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=capability,
                )
            ),
            "proposal_digest": _proposal_digest(proposal),
        },
        capability=capability,
        target_type=target_type,
        target_id=str(target_id),
        reason=reason,
        outcome=OperationsAuditEvent.Outcome.DRY_RUN,
        audit_summary={
            "operation": operation,
            "can_apply": bool(can_apply),
            "blocking_dependency_count": report["blocking_dependency_count"],
            "affected_records": report["affected_records"],
            "proposal_digest": _proposal_digest(proposal),
        },
    )


def _blocked(report, *, operation):
    if report["protected_object_status"]["protected"]:
        raise OperationsPermissionError(
            f"{operation} is blocked because this is a protected SHVYA object."
        )
    if report["migration_required"]:
        raise OperationsManualFixRequired(
            f"{operation} requires dependency migration first: "
            + " ".join(report["migration_requirements"])
        )


def archive_stage(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_STAGE_CONFIG_WRITE,
        tool_name="archive_stage",
        arguments=arguments,
    )
    stage = (
        Stage.objects.select_related("pipeline")
        .filter(
            pk=_uuid((arguments or {}).get("stage_id"), field="stage_id"),
            pipeline__organization=organization,
        )
        .first()
    )
    if stage is None:
        raise OperationsToolError("Stage not found in this organization.")
    report = _stage_report(organization=organization, stage=stage)
    proposal = {
        "stage_id": str(stage.id),
        "before": {"is_active": stage.is_active, "ai_on": stage.ai_on},
        "after": {"is_active": False, "ai_on": False},
        "dependency_snapshot": report,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return _lifecycle_dry_run(
            identity=identity,
            organization=organization,
            capability=CAP_STAGE_CONFIG_WRITE,
            target_type="stage",
            target_id=stage.id,
            reason=reason,
            operation="archive_stage",
            report=report,
            can_apply=report["can_archive"],
            reversible=True,
            proposal=proposal,
        )
    _blocked(report, operation="Stage archive")
    with transaction.atomic():
        locked = (
            Stage.objects.select_for_update()
            .select_related("pipeline")
            .get(pk=stage.pk, pipeline__organization=organization)
        )
        locked_report = _stage_report(organization=organization, stage=locked)
        locked_proposal = {
            "stage_id": str(locked.id),
            "before": {"is_active": locked.is_active, "ai_on": locked.ai_on},
            "after": {"is_active": False, "ai_on": False},
            "dependency_snapshot": locked_report,
        }
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=locked_proposal)
        _blocked(locked_report, operation="Stage archive")
        locked.is_active = False
        locked.ai_on = False
        locked.save(update_fields=["is_active", "ai_on", "updated_at"])
    return ToolExecution(
        data={
            "status": "ARCHIVED",
            **locked_report,
            "object": {**locked_report["object"], "active": False},
            "can_apply": True,
            "reversible": True,
            "restore_via": "upsert_stage_configuration",
            "verification": "passed",
        },
        capability=CAP_STAGE_CONFIG_WRITE,
        target_type="stage",
        target_id=str(stage.id),
        reason=reason,
        audit_summary={"operation": "archive_stage", "verification": "passed"},
    )


def delete_stage(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_STAGE_CONFIG_WRITE,
        tool_name="delete_stage",
        arguments=arguments,
    )
    stage = (
        Stage.objects.select_related("pipeline")
        .filter(
            pk=_uuid((arguments or {}).get("stage_id"), field="stage_id"),
            pipeline__organization=organization,
        )
        .first()
    )
    if stage is None:
        raise OperationsToolError("Stage not found in this organization.")
    report = _stage_report(organization=organization, stage=stage)
    proposal = {
        "stage_id": str(stage.id),
        "delete": True,
        "dependency_snapshot": report,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return _lifecycle_dry_run(
            identity=identity,
            organization=organization,
            capability=CAP_STAGE_CONFIG_WRITE,
            target_type="stage",
            target_id=stage.id,
            reason=reason,
            operation="delete_stage",
            report=report,
            can_apply=report["can_delete"],
            reversible=False,
            proposal=proposal,
        )
    _blocked(report, operation="Stage deletion")
    with transaction.atomic():
        locked = (
            Stage.objects.select_for_update()
            .select_related("pipeline")
            .get(pk=stage.pk, pipeline__organization=organization)
        )
        locked_report = _stage_report(organization=organization, stage=locked)
        locked_proposal = {
            "stage_id": str(locked.id),
            "delete": True,
            "dependency_snapshot": locked_report,
        }
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=locked_proposal)
        _blocked(locked_report, operation="Stage deletion")
        pipeline = locked.pipeline
        stage_id = str(locked.id)
        deleted, _ = locked.delete()
        if not deleted:
            raise OperationsPermissionError("Protected SHVYA stages cannot be deleted.")
        remaining = list(
            Stage.objects.select_for_update()
            .filter(pipeline=pipeline)
            .order_by("display_order", "name", "id")
        )
        offset = max([item.display_order for item in remaining] + [0]) + len(remaining) + 1000
        for index, item in enumerate(remaining, start=1):
            item.display_order = offset + index
        if remaining:
            Stage.objects.bulk_update(remaining, ["display_order"])
            for index, item in enumerate(remaining, start=1):
                item.display_order = index
            Stage.objects.bulk_update(remaining, ["display_order"])
    return ToolExecution(
        data={
            "status": "DELETED",
            "stage_id": stage_id,
            **report,
            "can_apply": True,
            "reversible": False,
            "verification": not Stage.objects.filter(pk=stage_id).exists(),
        },
        capability=CAP_STAGE_CONFIG_WRITE,
        target_type="stage",
        target_id=stage_id,
        reason=reason,
        audit_summary={"operation": "delete_stage", "verification": "passed"},
    )


def archive_attribute(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_ATTRIBUTE_CONFIG_WRITE,
        tool_name="archive_attribute",
        arguments=arguments,
    )
    attribute = AttributeDefinition.objects.filter(
        pk=_uuid((arguments or {}).get("attribute_id"), field="attribute_id"),
        organization=organization,
    ).first()
    if attribute is None:
        raise OperationsToolError("Attribute definition not found in this organization.")
    report = _attribute_report(organization=organization, attribute=attribute)
    proposal = {
        "attribute_id": str(attribute.id),
        "before_active": attribute.is_active,
        "after_active": False,
        "dependency_snapshot": report,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return _lifecycle_dry_run(
            identity=identity,
            organization=organization,
            capability=CAP_ATTRIBUTE_CONFIG_WRITE,
            target_type="attribute",
            target_id=attribute.id,
            reason=reason,
            operation="archive_attribute",
            report=report,
            can_apply=report["can_archive"],
            reversible=True,
            proposal=proposal,
        )
    _blocked(report, operation="Attribute archive")
    with transaction.atomic():
        locked = AttributeDefinition.objects.select_for_update().get(
            pk=attribute.pk,
            organization=organization,
        )
        locked_report = _attribute_report(organization=organization, attribute=locked)
        locked_proposal = {
            "attribute_id": str(locked.id),
            "before_active": locked.is_active,
            "after_active": False,
            "dependency_snapshot": locked_report,
        }
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=locked_proposal)
        _blocked(locked_report, operation="Attribute archive")
        locked.is_active = False
        locked.save(update_fields=["is_active", "updated_at"])
    return ToolExecution(
        data={
            "status": "ARCHIVED",
            **locked_report,
            "object": {**locked_report["object"], "active": False},
            "can_apply": True,
            "reversible": True,
            "restore_via": "upsert_attribute_configuration",
            "verification": "passed",
        },
        capability=CAP_ATTRIBUTE_CONFIG_WRITE,
        target_type="attribute",
        target_id=str(attribute.id),
        reason=reason,
        audit_summary={"operation": "archive_attribute", "verification": "passed"},
    )


def delete_attribute(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_ATTRIBUTE_CONFIG_WRITE,
        tool_name="delete_attribute",
        arguments=arguments,
    )
    attribute = AttributeDefinition.objects.filter(
        pk=_uuid((arguments or {}).get("attribute_id"), field="attribute_id"),
        organization=organization,
    ).first()
    if attribute is None:
        raise OperationsToolError("Attribute definition not found in this organization.")
    purge_values = (arguments or {}).get("purge_values", False)
    if not isinstance(purge_values, bool):
        raise OperationsToolError("purge_values must be a boolean.")

    report = _attribute_report(organization=organization, attribute=attribute)
    lead_values = report["affected_records"]["lead_values"]
    can_delete = (
        report["blocking_dependency_count"] == 0
        and (lead_values == 0 or purge_values)
        and lead_values <= ATTRIBUTE_PURGE_LIMIT
    )
    migration = list(report["migration_requirements"])
    if lead_values and not purge_values:
        migration.append(
            f"Clear/migrate {lead_values} lead value(s), or explicitly set purge_values=true."
        )
    if lead_values > ATTRIBUTE_PURGE_LIMIT:
        migration.append(
            f"Lead value purge exceeds the MCP safety limit of {ATTRIBUTE_PURGE_LIMIT}; use a dedicated migration."
        )
    report = {
        **report,
        "migration_required": bool(migration),
        "migration_requirements": migration,
        "can_delete": can_delete,
    }
    proposal = {
        "attribute_id": str(attribute.id),
        "delete": True,
        "purge_values": purge_values,
        "dependency_snapshot": report,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)

    if dry_run:
        return _lifecycle_dry_run(
            identity=identity,
            organization=organization,
            capability=CAP_ATTRIBUTE_CONFIG_WRITE,
            target_type="attribute",
            target_id=attribute.id,
            reason=reason,
            operation="delete_attribute",
            report=report,
            can_apply=can_delete,
            reversible=False,
            proposal=proposal,
        )

    if report["blocking_dependency_count"]:
        _blocked(report, operation="Attribute deletion")
    if lead_values > ATTRIBUTE_PURGE_LIMIT:
        raise OperationsManualFixRequired(
            f"Attribute deletion would purge {lead_values} lead values, above the "
            f"{ATTRIBUTE_PURGE_LIMIT} record safety limit."
        )
    if lead_values and not purge_values:
        raise OperationsManualFixRequired(
            "Attribute deletion requires explicit purge_values=true or prior value migration."
        )

    with transaction.atomic():
        locked = AttributeDefinition.objects.select_for_update().get(
            pk=attribute.pk,
            organization=organization,
        )
        locked_report = _attribute_report(organization=organization, attribute=locked)
        locked_values = locked_report["affected_records"]["lead_values"]
        locked_can_delete = (
            locked_report["blocking_dependency_count"] == 0
            and (locked_values == 0 or purge_values)
            and locked_values <= ATTRIBUTE_PURGE_LIMIT
        )
        locked_report = {
            **locked_report,
            "can_delete": locked_can_delete,
        }
        locked_proposal = {
            "attribute_id": str(locked.id),
            "delete": True,
            "purge_values": purge_values,
            "dependency_snapshot": {
                **locked_report,
                "migration_required": report["migration_required"],
                "migration_requirements": report["migration_requirements"],
            },
        }
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=locked_proposal)
        if locked_report["blocking_dependency_count"]:
            raise OperationsManualFixRequired(
                "Attribute dependencies changed after review. Run a fresh dry-run."
            )
        if locked_values > ATTRIBUTE_PURGE_LIMIT:
            raise OperationsManualFixRequired(
                "Affected lead count changed above the bounded purge limit."
            )
        if locked_values and not purge_values:
            raise OperationsManualFixRequired("Explicit purge_values=true is required.")
        attribute_id = str(locked.id)
        delete_attribute_definition(
            organization=organization,
            attribute=locked,
        )

    return ToolExecution(
        data={
            "status": "DELETED",
            "attribute_id": attribute_id,
            "purged_lead_values": lead_values,
            **report,
            "can_apply": True,
            "reversible": False,
            "verification": not AttributeDefinition.objects.filter(pk=attribute_id).exists(),
        },
        capability=CAP_ATTRIBUTE_CONFIG_WRITE,
        target_type="attribute",
        target_id=attribute_id,
        reason=reason,
        audit_summary={
            "operation": "delete_attribute",
            "purged_lead_values": lead_values,
            "verification": "passed",
        },
    )


def archive_pipeline(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_PIPELINE_CONFIG_WRITE,
        tool_name="archive_pipeline",
        arguments=arguments,
    )
    pipeline = Pipeline.objects.filter(
        pk=_uuid((arguments or {}).get("pipeline_id"), field="pipeline_id"),
        organization=organization,
    ).first()
    if pipeline is None:
        raise OperationsToolError("Pipeline not found in this organization.")
    report = _pipeline_report(organization=organization, pipeline=pipeline)
    proposal = {
        "pipeline_id": str(pipeline.id),
        "before": {"is_active": pipeline.is_active, "ai_enabled": pipeline.ai_enabled},
        "after": {"is_active": False, "ai_enabled": False},
        "dependency_snapshot": report,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return _lifecycle_dry_run(
            identity=identity,
            organization=organization,
            capability=CAP_PIPELINE_CONFIG_WRITE,
            target_type="pipeline",
            target_id=pipeline.id,
            reason=reason,
            operation="archive_pipeline",
            report=report,
            can_apply=report["can_archive"],
            reversible=True,
            proposal=proposal,
        )
    _blocked(report, operation="Pipeline archive")
    with transaction.atomic():
        locked = Pipeline.objects.select_for_update().get(
            pk=pipeline.pk,
            organization=organization,
        )
        locked_report = _pipeline_report(organization=organization, pipeline=locked)
        locked_proposal = {
            "pipeline_id": str(locked.id),
            "before": {"is_active": locked.is_active, "ai_enabled": locked.ai_enabled},
            "after": {"is_active": False, "ai_enabled": False},
            "dependency_snapshot": locked_report,
        }
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=locked_proposal)
        _blocked(locked_report, operation="Pipeline archive")
        locked.is_active = False
        locked.ai_enabled = False
        locked.save(update_fields=["is_active", "ai_enabled", "updated_at"])
    return ToolExecution(
        data={
            "status": "ARCHIVED",
            **locked_report,
            "object": {**locked_report["object"], "active": False},
            "can_apply": True,
            "reversible": True,
            "restore_via": "upsert_pipeline_configuration",
            "verification": "passed",
        },
        capability=CAP_PIPELINE_CONFIG_WRITE,
        target_type="pipeline",
        target_id=str(pipeline.id),
        reason=reason,
        audit_summary={"operation": "archive_pipeline", "verification": "passed"},
    )


def archive_cadence(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_CADENCE_CONFIG_WRITE,
        tool_name="archive_cadence",
        arguments=arguments,
    )
    cadence = FollowupSequence.objects.filter(
        pk=_uuid((arguments or {}).get("cadence_id"), field="cadence_id"),
        organization=organization,
    ).first()
    if cadence is None:
        raise OperationsToolError("Cadence not found in this organization.")
    report = _cadence_report(organization=organization, cadence=cadence)
    proposal = {
        "cadence_id": str(cadence.id),
        "before_active": cadence.is_active,
        "after_active": False,
        "dependency_snapshot": report,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return _lifecycle_dry_run(
            identity=identity,
            organization=organization,
            capability=CAP_CADENCE_CONFIG_WRITE,
            target_type="cadence",
            target_id=cadence.id,
            reason=reason,
            operation="archive_cadence",
            report=report,
            can_apply=report["can_archive"],
            reversible=True,
            proposal=proposal,
        )
    _blocked(report, operation="Cadence archive")
    with transaction.atomic():
        locked = FollowupSequence.objects.select_for_update().get(
            pk=cadence.pk,
            organization=organization,
        )
        locked_report = _cadence_report(organization=organization, cadence=locked)
        locked_proposal = {
            "cadence_id": str(locked.id),
            "before_active": locked.is_active,
            "after_active": False,
            "dependency_snapshot": locked_report,
        }
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=locked_proposal)
        _blocked(locked_report, operation="Cadence archive")
        locked.is_active = False
        locked.save(update_fields=["is_active", "updated_at"])
    return ToolExecution(
        data={
            "status": "ARCHIVED",
            **locked_report,
            "object": {**locked_report["object"], "active": False},
            "can_apply": True,
            "reversible": True,
            "restore_via": "upsert_cadence_configuration",
            "verification": "passed",
        },
        capability=CAP_CADENCE_CONFIG_WRITE,
        target_type="cadence",
        target_id=str(cadence.id),
        reason=reason,
        audit_summary={"operation": "archive_cadence", "verification": "passed"},
    )


def archive_workflow(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_WORKFLOW_CONFIG_WRITE,
        tool_name="archive_workflow",
        arguments=arguments,
    )
    workflow = SmartTrigger.objects.filter(
        pk=_uuid((arguments or {}).get("workflow_id"), field="workflow_id"),
        organization=organization,
    ).first()
    if workflow is None:
        raise OperationsToolError("Workflow not found in this organization.")
    report = _workflow_report(organization=organization, workflow=workflow)
    proposal = {
        "workflow_id": str(workflow.id),
        "before": {"is_active": workflow.is_active, "enabled": workflow.enabled},
        "after": {"is_active": False, "enabled": False},
        "dependency_snapshot": report,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return _lifecycle_dry_run(
            identity=identity,
            organization=organization,
            capability=CAP_WORKFLOW_CONFIG_WRITE,
            target_type="workflow",
            target_id=workflow.id,
            reason=reason,
            operation="archive_workflow",
            report=report,
            can_apply=True,
            reversible=report["affected_records"]["pending_or_queued_runs"] == 0,
            proposal=proposal,
        )
    with transaction.atomic():
        locked = SmartTrigger.objects.select_for_update().get(
            pk=workflow.pk,
            organization=organization,
        )
        locked_report = _workflow_report(organization=organization, workflow=locked)
        locked_proposal = {
            "workflow_id": str(locked.id),
            "before": {"is_active": locked.is_active, "enabled": locked.enabled},
            "after": {"is_active": False, "enabled": False},
            "dependency_snapshot": locked_report,
        }
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=locked_proposal)
        locked.is_active = False
        locked.enabled = False
        locked.save(update_fields=["is_active", "enabled", "updated_at"])
        TriggerRun.objects.filter(
            rule=locked,
            status__in=ACTIVE_RUN_STATUSES,
        ).update(
            status="skipped",
            detail="Workflow archived before execution.",
            finished_at=timezone.now(),
        )
    return ToolExecution(
        data={
            "status": "ARCHIVED",
            **locked_report,
            "object": {
                **locked_report["object"],
                "active": False,
                "enabled": False,
            },
            "can_apply": True,
            "reversible": (
                locked_report["affected_records"]["pending_or_queued_runs"] == 0
            ),
            "restore_via": "upsert_workflow_configuration",
            "verification": "passed",
        },
        capability=CAP_WORKFLOW_CONFIG_WRITE,
        target_type="workflow",
        target_id=str(workflow.id),
        reason=reason,
        audit_summary={
            "operation": "archive_workflow",
            "cancelled_pending_runs": locked_report["affected_records"][
                "pending_or_queued_runs"
            ],
            "verification": "passed",
        },
    )


LIFECYCLE_HANDLERS = {
    "archive_stage": archive_stage,
    "delete_stage": delete_stage,
    "archive_attribute": archive_attribute,
    "delete_attribute": delete_attribute,
    "archive_pipeline": archive_pipeline,
    "archive_cadence": archive_cadence,
    "archive_workflow": archive_workflow,
}
