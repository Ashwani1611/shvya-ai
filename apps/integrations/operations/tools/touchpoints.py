"""Touchpoint library tools for SHVYA Operations MCP."""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.db.models import Prefetch

from apps.followups.touchpoint_models import TouchpointCategory, TouchpointReply
from apps.integrations.operations_models import OperationsAuditEvent
from services.content_authoring import ContentAuthoringError, normalize_plain_text
from apps.integrations.operations_policy import (
    CAP_CADENCE_CONFIG_WRITE,
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
    _uuid,
    _write_gate,
)

def list_touchpoints(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity, organization=organization, capability=CAP_ORGANIZATION_READ
    )
    include_archived = bool((arguments or {}).get("include_archived", False))
    reply_qs = TouchpointReply.objects.all()
    if not include_archived:
        reply_qs = reply_qs.filter(is_active=True)
    categories = list(
        TouchpointCategory.objects.filter(organization=organization)
        .prefetch_related(Prefetch("replies", queryset=reply_qs))
        .order_by("name")[:100]
    )
    rows = [
        {
            "category_id": str(category.id),
            "category_name": category.name,
            "replies": [
                {
                    "id": str(reply.id),
                    "title": reply.title,
                    "body": reply.body,
                    "active": reply.is_active,
                    "updated_at": reply.updated_at.isoformat(),
                }
                for reply in category.replies.all()
            ],
        }
        for category in categories
    ]
    return ToolExecution(
        data={"categories": rows, "count": sum(len(row["replies"]) for row in rows)},
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={"touchpoint_count": sum(len(row["replies"]) for row in rows)},
    )


def _touchpoint_proposal(*, organization, arguments):
    data = (arguments or {}).get("data")
    if not isinstance(data, dict):
        raise OperationsToolError("data must be a Touchpoint object.")
    _reject_secret_like_content(data, field="touchpoint")
    touchpoint_id = str((arguments or {}).get("touchpoint_id") or "").strip()
    reply = None
    if touchpoint_id:
        reply = (
            TouchpointReply.objects.select_related("category")
            .filter(
                pk=_uuid(touchpoint_id, field="touchpoint_id"),
                category__organization=organization,
            )
            .first()
        )
        if reply is None:
            raise OperationsToolError("Touchpoint not found in this organization.")

    category_id = str(data.get("category_id") or "").strip()
    try:
        category_name = normalize_plain_text(
            data.get("category_name"),
            organization=organization,
            field="Touchpoint category",
            allow_placeholders=False,
            max_length=120,
        )
    except ContentAuthoringError as exc:
        raise OperationsToolError(str(exc)) from exc
    category = None
    if category_id:
        category = TouchpointCategory.objects.filter(
            pk=_uuid(category_id, field="category_id"),
            organization=organization,
        ).first()
        if category is None:
            raise OperationsToolError("Touchpoint category not found in this organization.")
    elif category_name:
        category = TouchpointCategory.objects.filter(
            organization=organization,
            name__iexact=category_name,
        ).first()
    elif reply:
        category = reply.category
    else:
        raise OperationsToolError("category_id or category_name is required.")

    try:
        title = normalize_plain_text(
            data.get("title", reply.title if reply else ""),
            organization=organization,
            field="Touchpoint title",
            allow_placeholders=False,
            required=True,
            max_length=150,
        )
        body = normalize_plain_text(
            data.get("body", reply.body if reply else ""),
            organization=organization,
            field="Touchpoint body",
            allow_placeholders=True,
            required=True,
            max_length=1000,
        )
    except ContentAuthoringError as exc:
        raise OperationsToolError(str(exc)) from exc

    resolved_category_name = category.name if category else category_name
    before = (
        {
            "id": str(reply.id),
            "category_id": str(reply.category_id),
            "category_name": reply.category.name,
            "title": reply.title,
            "body": reply.body,
            "active": reply.is_active,
        }
        if reply else None
    )
    after = {
        "category_id": str(category.id) if category else None,
        "category_name": resolved_category_name,
        "title": title,
        "body": body,
        "active": True,
    }
    return reply, category, before, after


def upsert_touchpoint(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_CADENCE_CONFIG_WRITE,
        tool_name="upsert_touchpoint",
        arguments=arguments,
    )
    reply, category, before, after = _touchpoint_proposal(
        organization=organization, arguments=arguments
    )
    proposal = {
        "organization_id": str(organization.id),
        "touchpoint_id": str(reply.id) if reply else None,
        "before": before,
        "after": after,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)

    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "operation": "update" if reply else "create",
                "after": after,
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_CADENCE_CONFIG_WRITE,
                ),
                "reversible": True,
            },
            capability=CAP_CADENCE_CONFIG_WRITE,
            target_type="touchpoint" if reply else "organization",
            target_id=str(reply.id) if reply else str(organization.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={
                "operation": "upsert_touchpoint",
                "proposal_digest": _proposal_digest(proposal),
            },
        )

    with transaction.atomic():
        organization.__class__.objects.select_for_update().get(pk=organization.pk)
        locked_reply, locked_category, locked_before, locked_after = _touchpoint_proposal(
            organization=organization, arguments=arguments
        )
        locked_proposal = {
            "organization_id": str(organization.id),
            "touchpoint_id": str(locked_reply.id) if locked_reply else None,
            "before": locked_before,
            "after": locked_after,
        }
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=locked_proposal)
        if locked_category is None:
            try:
                locked_category = TouchpointCategory.objects.create(
                    organization=organization,
                    name=locked_after["category_name"],
                )
            except IntegrityError:
                locked_category = TouchpointCategory.objects.get(
                    organization=organization,
                    name=locked_after["category_name"],
                )
        if locked_reply is None:
            locked_reply = TouchpointReply(category=locked_category)
        locked_reply.category = locked_category
        locked_reply.title = locked_after["title"]
        locked_reply.body = locked_after["body"]
        locked_reply.is_active = True
        locked_reply.full_clean()
        locked_reply.save()
        locked_reply.refresh_from_db()
        if (
            locked_reply.title != locked_after["title"]
            or locked_reply.body != locked_after["body"]
            or not locked_reply.is_active
        ):
            raise OperationsToolError("Touchpoint verification failed.")

    return ToolExecution(
        data={
            "status": "FIXED",
            "touchpoint": {
                "id": str(locked_reply.id),
                "category_id": str(locked_reply.category_id),
                "title": locked_reply.title,
                "body": locked_reply.body,
                "active": locked_reply.is_active,
            },
            "verification": "passed",
        },
        capability=CAP_CADENCE_CONFIG_WRITE,
        target_type="touchpoint",
        target_id=str(locked_reply.id),
        reason=reason,
        audit_summary={"operation": "upsert_touchpoint", "verification": "passed"},
    )


def archive_touchpoint(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_CADENCE_CONFIG_WRITE,
        tool_name="archive_touchpoint",
        arguments=arguments,
    )
    reply = (
        TouchpointReply.objects.select_related("category")
        .filter(
            pk=_uuid((arguments or {}).get("touchpoint_id"), field="touchpoint_id"),
            category__organization=organization,
        )
        .first()
    )
    if reply is None:
        raise OperationsToolError("Touchpoint not found in this organization.")
    proposal = {
        "touchpoint_id": str(reply.id),
        "before_active": reply.is_active,
        "after_active": False,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "touchpoint_id": str(reply.id),
                "dependencies": {},
                "affected_records": {"saved_reply_records": 1},
                "protected_object_status": {"protected": False, "reason": ""},
                "migration_required": False,
                "migration_requirements": [],
                "can_apply": True,
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_CADENCE_CONFIG_WRITE,
                ),
                "reversible": True,
            },
            capability=CAP_CADENCE_CONFIG_WRITE,
            target_type="touchpoint",
            target_id=str(reply.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={
                "operation": "archive_touchpoint",
                "proposal_digest": _proposal_digest(proposal),
            },
        )
    with transaction.atomic():
        locked = (
            TouchpointReply.objects.select_for_update()
            .filter(pk=reply.pk, category__organization=organization)
            .first()
        )
        if locked is None:
            raise OperationsApprovalRequired(
                "The Touchpoint changed after review. Run a fresh dry-run."
            )
        locked_proposal = {
            "touchpoint_id": str(locked.id),
            "before_active": locked.is_active,
            "after_active": False,
        }
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=locked_proposal)
        locked.is_active = False
        locked.save(update_fields=["is_active", "updated_at"])
    return ToolExecution(
        data={
            "status": "ARCHIVED",
            "touchpoint_id": str(reply.id),
            "active": False,
            "dependencies": {},
            "affected_records": {"saved_reply_records": 1},
            "protected_object_status": {"protected": False, "reason": ""},
            "migration_required": False,
            "migration_requirements": [],
            "can_apply": True,
            "reversible": True,
            "restore_via": "upsert_touchpoint",
            "verification": "passed",
        },
        capability=CAP_CADENCE_CONFIG_WRITE,
        target_type="touchpoint",
        target_id=str(reply.id),
        reason=reason,
        audit_summary={"operation": "archive_touchpoint", "verification": "passed"},
    )
