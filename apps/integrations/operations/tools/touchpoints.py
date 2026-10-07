"""Touchpoint library tools for SHVYA Operations MCP."""

from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import Prefetch

from apps.followups.touchpoint_models import TouchpointCategory, TouchpointReply
from services.touchpoint_service import validate_reply_placeholders
from apps.integrations.operations_models import OperationsAuditEvent
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
    arguments = arguments or {}
    if not isinstance(arguments, dict) or set(arguments) - {
        "include_archived", "category_id", "category_limit", "category_offset", "reply_limit", "reply_offset",
    }:
        raise OperationsToolError("Unsupported saved reply list arguments.")
    include_archived = arguments.get("include_archived", False)
    if type(include_archived) is not bool:
        raise OperationsToolError("include_archived must be a boolean.")
    values = {}
    for field, default, minimum, maximum in (
        ("category_limit", 20, 1, 20), ("category_offset", 0, 0, 1000000),
        ("reply_limit", 25, 1, 100), ("reply_offset", 0, 0, 1000000),
    ):
        value = arguments.get(field, default)
        if type(value) is not int or not minimum <= value <= maximum:
            raise OperationsToolError(f"{field} must be an integer from {minimum} to {maximum}.")
        values[field] = value
    category_id = arguments.get("category_id")
    if values["reply_offset"] and not category_id:
        raise OperationsToolError("A nonzero reply_offset requires one category_id.")
    if category_id and values["category_offset"]:
        raise OperationsToolError("category_offset cannot be combined with category_id.")
    category_qs = TouchpointCategory.objects.filter(organization=organization)
    if category_id:
        category_qs = category_qs.filter(pk=_uuid(category_id, field="category_id"))
        if not category_qs.exists():
            raise OperationsToolError("Saved reply category not found in this organization.")
    reply_qs = TouchpointReply.objects.filter(category__organization=organization)
    if not include_archived:
        reply_qs = reply_qs.filter(is_active=True)
    reply_offset, reply_limit = values["reply_offset"], values["reply_limit"]
    # Django implements this sliced prefetch with a per-category window limit;
    # a category with thousands of saved replies cannot produce an unbounded read.
    reply_qs = reply_qs.order_by("title", "id").prefetch_related("attachments")[reply_offset:reply_offset + reply_limit + 1]
    category_offset, category_limit = values["category_offset"], values["category_limit"]
    selected = list(category_qs.prefetch_related(
        Prefetch("replies", queryset=reply_qs, to_attr="mcp_replies")
    ).order_by("name", "id")[category_offset:category_offset + category_limit + 1])
    rows = []
    for category in selected[:category_limit]:
        has_more_replies = len(category.mcp_replies) > reply_limit
        rows.append({
            "category_id": str(category.pk), "category_name": category.name,
            "replies": [{"id": str(reply.pk), "title": reply.title, "body": reply.body,
                         "active": reply.is_active, "updated_at": reply.updated_at.isoformat(),
                         "attachments": [{"id": str(item.pk), "filename": item.original_name,
                                         "mime_type": item.mime_type, "size": item.size}
                                        for item in reply.attachments.all()]}
                        for reply in category.mcp_replies[:reply_limit]],
            "reply_offset": reply_offset, "has_more": has_more_replies,
            "next_reply_offset": reply_offset + reply_limit if has_more_replies else None,
        })
    more_categories = len(selected) > category_limit
    count = sum(len(row["replies"]) for row in rows)
    return ToolExecution(
        data={"categories": rows, "count": count, "category_count": len(rows),
              "category_offset": category_offset, "category_limit": category_limit, "reply_limit": reply_limit,
              "has_more": more_categories or any(row["has_more"] for row in rows),
              "has_more_categories": more_categories,
              "next_category_offset": category_offset + category_limit if more_categories else None},
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={"touchpoint_count": count, "category_count": len(rows)},
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
    category_name = str(data.get("category_name") or "").strip()
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

    title = str(data.get("title", reply.title if reply else "") or "").strip()
    body = str(data.get("body", reply.body if reply else "") or "").strip()
    if not title or len(title) > 150:
        raise OperationsToolError("Touchpoint title must be 1-150 characters.")
    if not body or len(body) > 1000:
        raise OperationsToolError("Touchpoint body must be 1-1000 characters.")
    try:
        validate_reply_placeholders(organization=organization, body=body)
    except DjangoValidationError as exc:
        raise OperationsToolError(" ".join(exc.messages)) from exc

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
