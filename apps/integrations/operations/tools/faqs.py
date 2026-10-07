"""AI Brain FAQ tools for SHVYA Operations MCP."""

from __future__ import annotations

from django.db import transaction

from apps.ai_engagement.models import FAQ
from apps.ai_engagement.services.faq import FAQService, FAQServiceError
from apps.integrations.operations_models import OperationsAuditEvent
from apps.integrations.operations_policy import (
    CAP_AI_CONFIG_WRITE,
    CAP_ORGANIZATION_READ,
    approval_required,
)
from apps.integrations.operations_tools import (
    OperationsApprovalRequired,
    OperationsToolError,
    ToolExecution,
    _ensure_approved_proposal_unchanged,
    _organization_for,
    _proposal_digest,
    _reject_secret_like_content,
    _require_operations_capability,
    _write_gate,
)

def list_faqs(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity, organization=organization, capability=CAP_ORGANIZATION_READ
    )
    arguments = arguments or {}
    if not isinstance(arguments, dict) or set(arguments) - {"active_only", "limit", "offset"}:
        raise OperationsToolError("Unsupported FAQ list arguments.")
    active_only = arguments.get("active_only", False)
    limit, offset = arguments.get("limit", 100), arguments.get("offset", 0)
    if type(active_only) is not bool:
        raise OperationsToolError("active_only must be a boolean.")
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 1000000:
        raise OperationsToolError("FAQ limit must be 1-100 and offset 0-1000000.")
    selected = list(FAQService().list(organization=organization, active_only=active_only)[offset:offset + limit + 1])
    rows = [
        {"id": str(item.id), "question": item.question, "answer": item.answer,
         "active": item.is_active, "updated_at": item.updated_at.isoformat()}
        for item in selected[:limit]
    ]
    has_more = len(selected) > limit
    return ToolExecution(
        data={"faqs": rows, "count": len(rows), "offset": offset, "limit": limit,
              "has_more": has_more, "next_offset": offset + limit if has_more else None},
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={"faq_count": len(rows), "has_more": has_more},
    )


def _faq_id(value):
    try:
        faq_id = int(str(value or "").strip())
    except (TypeError, ValueError) as exc:
        raise OperationsToolError("faq_id must be a positive integer.") from exc
    if faq_id <= 0:
        raise OperationsToolError("faq_id must be a positive integer.")
    return faq_id


def _faq_proposal(*, organization, arguments):
    data = (arguments or {}).get("data")
    if not isinstance(data, dict):
        raise OperationsToolError("data must be an FAQ object.")
    _reject_secret_like_content(data, field="faq")
    faq_id = str((arguments or {}).get("faq_id") or "").strip()
    faq = None
    if faq_id:
        faq = FAQ.objects.filter(
            pk=_faq_id(faq_id),
            organization=organization,
        ).first()
        if faq is None:
            raise OperationsToolError("FAQ not found in this organization.")
    question = str(data.get("question", faq.question if faq else "") or "").strip()
    answer = str(data.get("answer", faq.answer if faq else "") or "").strip()
    is_active = data.get("is_active", faq.is_active if faq else True)
    if not question or not answer:
        raise OperationsToolError("FAQ question and answer are required.")
    if not isinstance(is_active, bool):
        raise OperationsToolError("FAQ is_active must be a boolean.")
    before = (
        {"question": faq.question, "answer": faq.answer, "is_active": faq.is_active}
        if faq else None
    )
    after = {"question": question, "answer": answer, "is_active": is_active}
    return faq, before, after


def upsert_faq(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_AI_CONFIG_WRITE,
        tool_name="upsert_faq",
        arguments=arguments,
    )
    faq, before, after = _faq_proposal(organization=organization, arguments=arguments)
    proposal = {
        "organization_id": str(organization.id),
        "faq_id": str(faq.id) if faq else None,
        "before": before,
        "after": after,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "operation": "update" if faq else "create",
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_AI_CONFIG_WRITE,
                ),
                "reversible": True,
            },
            capability=CAP_AI_CONFIG_WRITE,
            target_type="faq" if faq else "organization",
            target_id=str(faq.id) if faq else str(organization.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={
                "operation": "upsert_faq",
                "proposal_digest": _proposal_digest(proposal),
            },
        )

    service = FAQService()
    with transaction.atomic():
        organization.__class__.objects.select_for_update().get(pk=organization.pk)
        locked_faq, locked_before, locked_after = _faq_proposal(
            organization=organization, arguments=arguments
        )
        locked_proposal = {
            "organization_id": str(organization.id),
            "faq_id": str(locked_faq.id) if locked_faq else None,
            "before": locked_before,
            "after": locked_after,
        }
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=locked_proposal)
        try:
            if locked_faq:
                saved = service.update(
                    organization=organization,
                    faq_id=locked_faq.id,
                    data=locked_after,
                )
            else:
                saved = service.create(
                    organization=organization,
                    question=locked_after["question"],
                    answer=locked_after["answer"],
                    is_active=locked_after["is_active"],
                )
        except FAQServiceError as exc:
            raise OperationsToolError(str(exc)) from exc
    saved.refresh_from_db()
    return ToolExecution(
        data={
            "status": "FIXED",
            "faq": {
                "id": str(saved.id),
                "question": saved.question,
                "answer": saved.answer,
                "active": saved.is_active,
            },
            "verification": "passed",
        },
        capability=CAP_AI_CONFIG_WRITE,
        target_type="faq",
        target_id=str(saved.id),
        reason=reason,
        audit_summary={"operation": "upsert_faq", "verification": "passed"},
    )


def archive_faq(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_AI_CONFIG_WRITE,
        tool_name="archive_faq",
        arguments=arguments,
    )
    faq = FAQ.objects.filter(
        pk=_faq_id((arguments or {}).get("faq_id")),
        organization=organization,
    ).first()
    if faq is None:
        raise OperationsToolError("FAQ not found in this organization.")
    proposal = {"faq_id": str(faq.id), "before_active": faq.is_active, "after_active": False}
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "faq_id": str(faq.id),
                "dependencies": {},
                "affected_records": {"faq_records": 1},
                "protected_object_status": {"protected": False, "reason": ""},
                "migration_required": False,
                "migration_requirements": [],
                "can_apply": True,
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_AI_CONFIG_WRITE,
                ),
                "reversible": True,
            },
            capability=CAP_AI_CONFIG_WRITE,
            target_type="faq",
            target_id=str(faq.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={"operation": "archive_faq", "proposal_digest": _proposal_digest(proposal)},
        )
    with transaction.atomic():
        locked = FAQ.objects.select_for_update().filter(
            pk=faq.pk, organization=organization
        ).first()
        if locked is None:
            raise OperationsApprovalRequired("The FAQ changed after review. Run a fresh dry-run.")
        locked_proposal = {"faq_id": str(locked.id), "before_active": locked.is_active, "after_active": False}
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=locked_proposal)
        try:
            saved = FAQService().deactivate(organization=organization, faq_id=locked.id)
        except FAQServiceError as exc:
            raise OperationsToolError(str(exc)) from exc
    return ToolExecution(
        data={
            "status": "ARCHIVED",
            "faq_id": str(saved.id),
            "active": saved.is_active,
            "dependencies": {},
            "affected_records": {"faq_records": 1},
            "protected_object_status": {"protected": False, "reason": ""},
            "migration_required": False,
            "migration_requirements": [],
            "can_apply": True,
            "reversible": True,
            "restore_via": "upsert_faq",
            "verification": "passed",
        },
        capability=CAP_AI_CONFIG_WRITE,
        target_type="faq",
        target_id=str(saved.id),
        reason=reason,
        audit_summary={"operation": "archive_faq", "verification": "passed"},
    )
