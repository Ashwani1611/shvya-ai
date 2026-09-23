"""WhatsApp account and routing tools for SHVYA Operations MCP."""

from __future__ import annotations

from django.db import transaction

from apps.channels.models import WhatsAppAccount
from apps.crm.models import Pipeline
from apps.integrations.operations_models import OperationsAuditEvent
from apps.integrations.operations_policy import (
    CAP_MESSAGING_CONFIG_WRITE,
    CAP_ORGANIZATION_READ,
    approval_required,
)
from apps.organizations.features import is_hosted_account_enabled
from services.channels.hosted_whatsapp_service import (
    HostedWhatsAppValidationError,
    create_hosted_account,
    get_pipeline_for_account,
    normalize_whatsapp_number,
    pipeline_whatsapp_number,
    require_pipeline_number,
)
from apps.integrations.operations_tools import (
    OperationsApprovalRequired,
    OperationsPermissionError,
    OperationsToolError,
    ToolExecution,
    _ensure_approved_proposal_unchanged,
    _organization_for,
    _proposal_digest,
    _require_operations_capability,
    _uuid,
    _write_gate,
)

def list_whatsapp_accounts(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_ORGANIZATION_READ,
    )
    accounts = list(
        WhatsAppAccount.objects.filter(
            organization=organization,
            is_active=True,
        )
        .defer("access_token")
        .order_by("connection_type", "business_name", "id")[:100]
    )
    rows = []
    for account in accounts:
        pipeline = get_pipeline_for_account(account=account)
        rows.append(
            {
                "id": str(account.id),
                "connection_type": account.connection_type,
                "business_name": account.business_name,
                "display_phone_number": account.display_phone_number,
                "status": account.status,
                "pipeline": (
                    {"id": str(pipeline.id), "name": pipeline.name}
                    if pipeline else None
                ),
                "routing_valid": pipeline is not None,
            }
        )
    return ToolExecution(
        data={"accounts": rows, "count": len(rows)},
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={"account_count": len(rows)},
    )


def _routing_snapshot(organization):
    pipelines = list(
        Pipeline.objects.filter(organization=organization, is_active=True)
        .only("id", "name", "country_code", "phone_number")
        .order_by("name")
    )
    by_number = {}
    for pipeline in pipelines:
        number = pipeline_whatsapp_number(pipeline)
        if number:
            by_number.setdefault(number, []).append(pipeline)

    conflicts = [
        {
            "phone_number": number,
            "pipeline_ids": [str(item.id) for item in values],
            "pipeline_names": [item.name for item in values],
        }
        for number, values in by_number.items()
        if len(values) > 1
    ]
    accounts = list(
        WhatsAppAccount.objects.filter(organization=organization, is_active=True)
        .defer("access_token")
        .order_by("id")
    )
    account_rows = []
    for account in accounts:
        number = normalize_whatsapp_number(
            phone_number=account.display_phone_number or account.phone_number_id
        )
        matches = by_number.get(number, [])
        account_rows.append(
            {
                "account_id": str(account.id),
                "connection_type": account.connection_type,
                "display_phone_number": account.display_phone_number,
                "status": account.status,
                "pipeline_id": str(matches[0].id) if len(matches) == 1 else None,
                "pipeline_name": matches[0].name if len(matches) == 1 else None,
                "match_count": len(matches),
                "valid": len(matches) == 1,
            }
        )
    return {
        "valid": not conflicts and all(row["valid"] for row in account_rows),
        "conflicts": conflicts,
        "accounts": account_rows,
        "active_pipeline_count": len(pipelines),
    }


def validate_whatsapp_routing(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_ORGANIZATION_READ,
    )
    snapshot = _routing_snapshot(organization)
    return ToolExecution(
        data=snapshot,
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={
            "valid": snapshot["valid"],
            "conflict_count": len(snapshot["conflicts"]),
            "account_count": len(snapshot["accounts"]),
        },
    )


def bind_whatsapp_account_to_pipeline(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_MESSAGING_CONFIG_WRITE,
        tool_name="bind_whatsapp_account_to_pipeline",
        arguments=arguments,
    )
    pipeline = (
        Pipeline.objects.filter(
            pk=_uuid((arguments or {}).get("pipeline_id"), field="pipeline_id"),
            organization=organization,
            is_active=True,
        ).first()
    )
    if pipeline is None:
        raise OperationsToolError("Active pipeline not found in this organization.")

    account_id = str((arguments or {}).get("whatsapp_account_id") or "").strip()
    account = None
    if account_id:
        account = (
            WhatsAppAccount.objects.filter(
                pk=_uuid(account_id, field="whatsapp_account_id"),
                organization=organization,
                is_active=True,
            )
            .defer("access_token")
            .first()
        )
        if account is None:
            raise OperationsToolError("Active WhatsApp account not found in this organization.")

    country_code = str((arguments or {}).get("country_code") or pipeline.country_code or "").strip()
    phone_number = str((arguments or {}).get("phone_number") or "").strip()
    if account is not None and not phone_number:
        phone_number = account.display_phone_number or account.phone_number_id

    normalized = normalize_whatsapp_number(
        country_code=country_code,
        phone_number=phone_number,
    )
    if not normalized:
        raise OperationsToolError("A valid WhatsApp phone number including country code is required.")

    if account is not None:
        account_number = normalize_whatsapp_number(
            phone_number=account.display_phone_number or account.phone_number_id
        )
        if account_number and account_number != normalized:
            raise OperationsToolError(
                "The requested pipeline number does not match the selected WhatsApp account."
            )

    conflicts = [
        item
        for item in Pipeline.objects.filter(
            organization=organization,
            is_active=True,
        ).exclude(pk=pipeline.pk)
        if pipeline_whatsapp_number(item) == normalized
    ]
    if conflicts:
        raise OperationsToolError(
            "This WhatsApp number is already mapped to another active pipeline."
        )

    before = {
        "country_code": pipeline.country_code,
        "phone_number": pipeline.phone_number,
    }
    after = {
        "country_code": country_code,
        "phone_number": normalized,
    }
    proposal = {
        "pipeline_id": str(pipeline.id),
        "whatsapp_account_id": str(account.id) if account else None,
        "before": before,
        "after": after,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)

    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "pipeline_id": str(pipeline.id),
                "display_phone_number": normalized,
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_MESSAGING_CONFIG_WRITE,
                ),
                "routing_conflicts": 0,
                "reversible": True,
            },
            capability=CAP_MESSAGING_CONFIG_WRITE,
            target_type="pipeline",
            target_id=str(pipeline.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={
                "operation": "bind_whatsapp_account_to_pipeline",
                "proposal_digest": _proposal_digest(proposal),
            },
        )

    with transaction.atomic():
        locked = (
            Pipeline.objects.select_for_update()
            .filter(pk=pipeline.pk, organization=organization, is_active=True)
            .first()
        )
        if locked is None:
            raise OperationsApprovalRequired(
                "The pipeline changed or became inactive. Run a fresh dry-run."
            )
        locked_before = {
            "country_code": locked.country_code,
            "phone_number": locked.phone_number,
        }
        locked_proposal = {
            "pipeline_id": str(locked.id),
            "whatsapp_account_id": str(account.id) if account else None,
            "before": locked_before,
            "after": after,
        }
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=locked_proposal)
        for item in Pipeline.objects.select_for_update().filter(
            organization=organization, is_active=True
        ).exclude(pk=locked.pk):
            if pipeline_whatsapp_number(item) == normalized:
                raise OperationsApprovalRequired(
                    "WhatsApp routing changed after review. Run a fresh dry-run."
                )
        locked.country_code = country_code
        locked.phone_number = normalized
        locked.save(update_fields=["country_code", "phone_number", "updated_at"])
        if pipeline_whatsapp_number(locked) != normalized:
            raise OperationsToolError("WhatsApp pipeline binding verification failed.")

    return ToolExecution(
        data={
            "status": "FIXED",
            "pipeline_id": str(pipeline.id),
            "display_phone_number": normalized,
            "verification": "passed",
        },
        capability=CAP_MESSAGING_CONFIG_WRITE,
        target_type="pipeline",
        target_id=str(pipeline.id),
        reason=reason,
        audit_summary={
            "operation": "bind_whatsapp_account_to_pipeline",
            "verification": "passed",
        },
    )


def begin_whatsapp_connection(*, identity, arguments):
    organization = _organization_for(identity)
    if not is_hosted_account_enabled(organization):
        raise OperationsPermissionError(
            "Hosted WhatsApp is not enabled for this organization."
        )
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_MESSAGING_CONFIG_WRITE,
        tool_name="begin_whatsapp_connection",
        arguments=arguments,
    )
    country_code = str((arguments or {}).get("country_code") or "").strip()
    phone_number = str((arguments or {}).get("phone_number") or "").strip()
    try:
        normalized, pipeline = require_pipeline_number(
            organization=organization,
            country_code=country_code,
            phone_number=phone_number,
        )
    except HostedWhatsAppValidationError as exc:
        raise OperationsToolError(str(exc)) from exc

    existing = (
        WhatsAppAccount.objects.filter(
            organization=organization,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            display_phone_number=normalized,
            is_active=True,
        )
        .defer("access_token")
        .first()
    )
    proposal = {
        "organization_id": str(organization.id),
        "pipeline_id": str(pipeline.id),
        "display_phone_number": normalized,
        "existing_account_id": str(existing.id) if existing else None,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)

    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "pipeline": {"id": str(pipeline.id), "name": pipeline.name},
                "display_phone_number": normalized,
                "existing_account": bool(existing),
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_MESSAGING_CONFIG_WRITE,
                ),
                "requires_human_qr_scan": True,
                "safe_status_only": True,
            },
            capability=CAP_MESSAGING_CONFIG_WRITE,
            target_type="pipeline",
            target_id=str(pipeline.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={
                "operation": "begin_whatsapp_connection",
                "proposal_digest": _proposal_digest(proposal),
            },
        )

    from apps.channels.hosted_tasks import initialize_hosted_session_task

    with transaction.atomic():
        locked_organization = organization.__class__.objects.select_for_update().get(
            pk=organization.pk
        )
        try:
            normalized_locked, pipeline_locked = require_pipeline_number(
                organization=locked_organization,
                country_code=country_code,
                phone_number=phone_number,
            )
        except HostedWhatsAppValidationError as exc:
            raise OperationsApprovalRequired(
                "Hosted WhatsApp routing changed after review. Run a fresh dry-run."
            ) from exc
        existing_locked = (
            WhatsAppAccount.objects.select_for_update()
            .filter(
                organization=locked_organization,
                connection_type=WhatsAppAccount.ConnectionType.coexisted,
                display_phone_number=normalized_locked,
                is_active=True,
            )
            .defer("access_token")
            .first()
        )
        locked_proposal = {
            "organization_id": str(locked_organization.id),
            "pipeline_id": str(pipeline_locked.id),
            "display_phone_number": normalized_locked,
            "existing_account_id": str(existing_locked.id) if existing_locked else None,
        }
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=locked_proposal)
        try:
            account, pipeline_locked, created = create_hosted_account(
                organization=locked_organization,
                created_by=identity.actor,
                country_code=country_code,
                phone_number=phone_number,
            )
        except HostedWhatsAppValidationError as exc:
            raise OperationsToolError(str(exc)) from exc
        transaction.on_commit(
            lambda account_id=str(account.id): initialize_hosted_session_task.delay(account_id)
        )

    account.refresh_from_db()
    return ToolExecution(
        data={
            "status": "FIXED",
            "created": created,
            "account": {
                "id": str(account.id),
                "connection_type": account.connection_type,
                "display_phone_number": account.display_phone_number,
                "status": account.status,
            },
            "pipeline": {"id": str(pipeline_locked.id), "name": pipeline_locked.name},
            "requires_human_qr_scan": account.status != WhatsAppAccount.Status.CONNECTED,
            "safe_status_only": True,
            "verification": "passed",
        },
        capability=CAP_MESSAGING_CONFIG_WRITE,
        target_type="whatsapp_account",
        target_id=str(account.id),
        reason=reason,
        audit_summary={
            "operation": "begin_whatsapp_connection",
            "created": created,
            "pipeline_id": str(pipeline_locked.id),
            "verification": "passed",
        },
    )
