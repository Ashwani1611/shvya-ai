"""Durable follow-up commitments for onboarding, integrations and audits."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from apps.integrations.diagnostic_auth import sanitize_text
from apps.integrations.operations_models import OperationsAuditEvent, OperationsCommitment
from apps.integrations.operations_policy import CAP_OPERATIONS_TASK_WRITE, CAP_ORGANIZATION_READ, approval_required
from apps.integrations.operations_tools import (
    OperationsToolError,
    ToolExecution,
    _ensure_approved_proposal_unchanged,
    _organization_for,
    _proposal_digest,
    _reject_secret_like_content,
    _require_operations_capability,
    _uuid,
    _write_gate,
)


def _snapshot(item):
    return {
        "id": str(item.id),
        "source": item.source,
        "title": item.title,
        "description": item.description,
        "status": item.status,
        "due_at": item.due_at.isoformat() if item.due_at else None,
        "source_reference": item.source_reference,
        "resolution": item.resolution,
        "owner_id": str(item.owner_id) if item.owner_id else None,
        "metadata": item.metadata if isinstance(item.metadata, dict) else {},
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def list_commitments(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(identity=identity, organization=organization, capability=CAP_ORGANIZATION_READ)
    qs = OperationsCommitment.objects.filter(organization=organization).select_related("owner")
    status = str((arguments or {}).get("status") or "").strip()
    if status:
        qs = qs.filter(status=status)
    rows = [_snapshot(item) for item in qs.order_by("status", "due_at", "-created_at")[:200]]
    return ToolExecution(data={"commitments": rows, "count": len(rows), "tenant_isolation": "organization_id enforced"}, capability=CAP_ORGANIZATION_READ, target_type="organization", target_id=str(organization.id))


def upsert_commitment(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(identity=identity, organization=organization, capability=CAP_OPERATIONS_TASK_WRITE, tool_name="upsert_commitment", arguments=arguments)
    data = (arguments or {}).get("data")
    if not isinstance(data, dict):
        raise OperationsToolError("data must be an object.")
    allowed = {"source", "title", "description", "status", "due_at", "source_reference", "resolution", "owner_id", "metadata"}
    if set(data) - allowed:
        raise OperationsToolError("Unsupported commitment field.")
    if "source" in data and data.get("source") not in {choice[0] for choice in OperationsCommitment.Source.choices}:
        raise OperationsToolError("source is not supported.")
    if "status" in data and data.get("status") not in {choice[0] for choice in OperationsCommitment.Status.choices}:
        raise OperationsToolError("status is not supported.")
    if "due_at" in data and data["due_at"] is not None:
        try:
            data["due_at"] = datetime.fromisoformat(str(data["due_at"]).replace("Z", "+00:00"))
        except ValueError as exc:
            raise OperationsToolError("due_at must be an ISO datetime or null.") from exc
    title = str(data.get("title") or "").strip()
    if not title or len(title) > 200:
        raise OperationsToolError("title is required and must be at most 200 characters.")
    for field in ("title", "description", "resolution", "source_reference"):
        value = str(data.get(field) or "")
        if value and sanitize_text(value, limit=max(500, len(value) + 1), redact_long=True) != value:
            raise OperationsToolError("Commitment text contains credential-like material.")
    item = None
    if (arguments or {}).get("commitment_id"):
        item = OperationsCommitment.objects.filter(pk=_uuid(arguments["commitment_id"], field="commitment_id"), organization=organization).first()
        if item is None:
            raise OperationsToolError("Commitment not found in this organization.")
    if item is None and not data.get("source"):
        raise OperationsToolError("source is required when creating a commitment.")
    before = _snapshot(item) if item else None
    after = deepcopy(before or {})
    after.update({key: value for key, value in data.items() if key != "metadata"})
    if "metadata" in data:
        if not isinstance(data["metadata"], dict):
            raise OperationsToolError("metadata must be an object.")
        _reject_secret_like_content(data["metadata"], field="commitment.metadata")
        after["metadata"] = data["metadata"]
    proposal = {"commitment_id": str(item.id) if item else None, "before": before, "after": after}
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return ToolExecution(data={"status": "DRY_RUN", "operation": "update" if item else "create", "after": after, "approval_required": approval_required(role=identity.role, organization=organization, capability=CAP_OPERATIONS_TASK_WRITE), "reversible": True}, capability=CAP_OPERATIONS_TASK_WRITE, target_type="commitment", target_id=str(item.id) if item else str(organization.id), reason=reason, outcome=OperationsAuditEvent.Outcome.DRY_RUN, audit_summary={"proposal_digest": _proposal_digest(proposal)})
    with transaction.atomic():
        locked = OperationsCommitment.objects.select_for_update().filter(pk=item.pk, organization=organization).first() if item else OperationsCommitment()
        if item and locked is None:
            raise OperationsToolError("Commitment changed before execution; run a fresh dry-run.")
        if not item:
            locked.organization = organization
            locked.created_by = identity.actor
        for field in allowed - {"owner_id", "metadata"}:
            if field in data:
                setattr(locked, field, data[field])
        if "owner_id" in data and data["owner_id"] is not None:
            owner = identity.actor.__class__.objects.filter(pk=_uuid(data["owner_id"], field="owner_id"), organization=organization).first()
            if owner is None:
                raise OperationsToolError("owner_id must belong to this organization.")
            locked.owner = owner
        elif "owner_id" in data:
            locked.owner = None
        if "metadata" in data:
            locked.metadata = data["metadata"]
        if locked.status == OperationsCommitment.Status.COMPLETED and not locked.completed_at:
            locked.completed_at = timezone.now()
        locked.full_clean()
        locked.save()
        result = _snapshot(locked)
    return ToolExecution(data={"status": "UPDATED", "commitment": result, "verification": "passed"}, capability=CAP_OPERATIONS_TASK_WRITE, target_type="commitment", target_id=result["id"], reason=reason, audit_summary={"verification": "passed"})
