"""WhatsApp account and routing tools for SHVYA Operations MCP."""

from __future__ import annotations

from django.db import IntegrityError, transaction

from apps.channels.models import WhatsAppAccount, WhatsAppTemplate
from apps.channels.template_models import WhatsAppTemplateMetadata
from apps.crm.models import Pipeline
from apps.integrations.operations_models import OperationsAuditEvent
from apps.integrations.operations_policy import (
    CAP_MESSAGING_CONFIG_WRITE,
    CAP_ORGANIZATION_READ,
    approval_required,
)
from apps.organizations.features import is_hosted_account_enabled
from services.channels.template_service import (
    TemplateError,
    create_template,
    state_for,
    submit_template,
)
from services.content_authoring import ContentAuthoringError, normalize_plain_text
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


def _template_account(*, organization, account_id):
    account = (
        WhatsAppAccount.objects.filter(
            pk=_uuid(account_id, field="whatsapp_account_id"),
            organization=organization,
            is_active=True,
        )
        .first()
    )
    if account is None:
        raise OperationsToolError("Active WhatsApp account not found in this organization.")
    if account.status != WhatsAppAccount.Status.CONNECTED:
        raise OperationsToolError("The selected WhatsApp account is not connected.")
    if not account.waba_id or not account.phone_number_id or not account.access_token:
        raise OperationsToolError(
            "The selected WhatsApp account is not Meta template-capable. "
            "A connected Meta WABA, phone number ID, and access token are required."
        )
    return account


def _template_snapshot(template):
    metadata = (
        WhatsAppTemplateMetadata.objects.filter(template=template)
        .only("language", "local_status", "meta_error_code", "meta_error_message")
        .first()
    )
    pipeline = get_pipeline_for_account(account=template.account)
    return {
        "id": str(template.id),
        "name": template.name,
        "category": template.category,
        "template_format": template.template_format,
        "status": template.status,
        "language": (metadata.language if metadata else "") or "en_US",
        "body": template.body,
        "footer": template.footer,
        "attachment_type": template.attachment_type,
        "buttons": template.buttons or [],
        "meta_template_id": template.meta_template_id or "",
        "rejection_reason": template.rejection_reason or "",
        "local_status": metadata.local_status if metadata else "",
        "meta_error_code": metadata.meta_error_code if metadata else "",
        "meta_error_message": metadata.meta_error_message if metadata else "",
        "whatsapp_account": {
            "id": str(template.account_id),
            "business_name": template.account.business_name,
            "display_phone_number": template.account.display_phone_number,
            "connection_type": template.account.connection_type,
        },
        "pipeline": (
            {"id": str(pipeline.id), "name": pipeline.name}
            if pipeline is not None
            else None
        ),
        "created_at": template.created_at.isoformat() if template.created_at else None,
        "updated_at": template.updated_at.isoformat() if template.updated_at else None,
    }


def list_whatsapp_templates(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_ORGANIZATION_READ,
    )
    queryset = (
        WhatsAppTemplate.objects.filter(organization=organization)
        .select_related("account")
        .order_by("-updated_at")
    )
    account_id = str((arguments or {}).get("whatsapp_account_id") or "").strip()
    if account_id:
        account = _template_account(organization=organization, account_id=account_id)
        queryset = queryset.filter(account=account)
    status = str((arguments or {}).get("status") or "").strip().lower()
    if status:
        allowed = {value for value, _label in WhatsAppTemplate.Status.choices}
        if status not in allowed:
            raise OperationsToolError("Invalid WhatsApp template status.")
        queryset = queryset.filter(status=status)
    limit = int((arguments or {}).get("limit") or 50)
    limit = min(max(limit, 1), 100)
    rows = [_template_snapshot(item) for item in queryset[:limit]]
    return ToolExecution(
        data={"templates": rows, "count": len(rows)},
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={"template_count": len(rows)},
    )


def get_whatsapp_template_status(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity,
        organization=organization,
        capability=CAP_ORGANIZATION_READ,
    )
    template = (
        WhatsAppTemplate.objects.filter(
            pk=_uuid((arguments or {}).get("template_id"), field="template_id"),
            organization=organization,
        )
        .select_related("account")
        .first()
    )
    if template is None:
        raise OperationsToolError("WhatsApp template not found in this organization.")
    return ToolExecution(
        data={"template": _template_snapshot(template)},
        capability=CAP_ORGANIZATION_READ,
        target_type="whatsapp_template",
        target_id=str(template.id),
        audit_summary={"status": template.status},
    )


def _template_create_values(*, organization, arguments):
    category = str((arguments or {}).get("category") or WhatsAppTemplate.Category.MARKETING).strip().lower()
    allowed_categories = {value for value, _label in WhatsAppTemplate.Category.choices}
    if category not in allowed_categories:
        raise OperationsToolError("Invalid WhatsApp template category.")
    try:
        body = normalize_plain_text(
            (arguments or {}).get("body"),
            organization=organization,
            field="Template body",
            allow_placeholders=True,
            required=True,
            max_length=1024,
        )
        footer = normalize_plain_text(
            (arguments or {}).get("footer"),
            organization=organization,
            field="Template footer",
            allow_placeholders=False,
            max_length=60,
        )
        buttons = []
        for item in list((arguments or {}).get("buttons") or []):
            clean = dict(item or {})
            if "text" in clean:
                clean["text"] = normalize_plain_text(
                    clean.get("text"),
                    organization=organization,
                    field="Template button text",
                    allow_placeholders=False,
                    max_length=25,
                )
            if "coupon_code" in clean:
                clean["coupon_code"] = normalize_plain_text(
                    clean.get("coupon_code"),
                    organization=organization,
                    field="Template coupon code",
                    allow_placeholders=False,
                    max_length=15,
                )
            buttons.append(clean)
    except ContentAuthoringError as exc:
        raise OperationsToolError(str(exc)) from exc
    return {
        "name": str((arguments or {}).get("name") or "").strip(),
        "body": body,
        "category": category,
        "footer": footer,
        "buttons": buttons,
        "language": str((arguments or {}).get("language") or "en_US").strip() or "en_US",
    }


def _existing_template_matches(*, template, values):
    metadata = WhatsAppTemplateMetadata.objects.filter(template=template).only("language").first()
    return (
        template.body == values["body"]
        and template.category == values["category"]
        and template.template_format == WhatsAppTemplate.Format.STANDARD
        and template.footer == values["footer"]
        and template.attachment_type == WhatsAppTemplate.AttachmentType.NONE
        and (template.buttons or []) == values["buttons"]
        and (((metadata.language if metadata else "") or "en_US") == values["language"])
    )


def create_whatsapp_template(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_MESSAGING_CONFIG_WRITE,
        tool_name="create_whatsapp_template",
        arguments=arguments,
    )
    account = _template_account(
        organization=organization,
        account_id=(arguments or {}).get("whatsapp_account_id"),
    )
    pipeline_id = str((arguments or {}).get("pipeline_id") or "").strip()
    pipeline = get_pipeline_for_account(account=account)
    if pipeline_id and (pipeline is None or str(pipeline.id) != pipeline_id):
        raise OperationsToolError(
            "The selected WhatsApp account is not routed to the requested pipeline."
        )
    values = _template_create_values(organization=organization, arguments=arguments)
    if not values["name"] or not values["body"]:
        raise OperationsToolError("Template name and body are required.")

    existing = (
        WhatsAppTemplate.objects.filter(account=account, name=values["name"])
        .select_related("account")
        .first()
    )
    if existing is not None:
        if not _existing_template_matches(template=existing, values=values):
            raise OperationsToolError(
                "A template with this name already exists for the selected WhatsApp account "
                "with different content."
            )
        return ToolExecution(
            data={
                "status": "NO_CHANGE",
                "template": _template_snapshot(existing),
                "idempotent": True,
            },
            capability=CAP_MESSAGING_CONFIG_WRITE,
            target_type="whatsapp_template",
            target_id=str(existing.id),
            reason=reason,
            outcome=(
                OperationsAuditEvent.Outcome.DRY_RUN
                if dry_run
                else OperationsAuditEvent.Outcome.SUCCESS
            ),
            audit_summary={
                "operation": "create_whatsapp_template",
                "idempotent": True,
            },
        )

    proposal = {
        "organization_id": str(organization.id),
        "whatsapp_account_id": str(account.id),
        "pipeline_id": str(pipeline.id) if pipeline else None,
        "name": values["name"],
        "body": values["body"],
        "category": values["category"],
        "footer": values["footer"],
        "buttons": values["buttons"],
        "language": values["language"],
        "template_format": WhatsAppTemplate.Format.STANDARD,
        "attachment_type": WhatsAppTemplate.AttachmentType.NONE,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)

    if dry_run:
        try:
            with transaction.atomic():
                preview = create_template(
                    organization=organization,
                    account=account,
                    created_by=identity.actor,
                    name=values["name"],
                    body=values["body"],
                    category=values["category"],
                    template_format=WhatsAppTemplate.Format.STANDARD,
                    footer=values["footer"],
                    attachment_type=WhatsAppTemplate.AttachmentType.NONE,
                    buttons=values["buttons"],
                    language=values["language"],
                )
                preview_data = _template_snapshot(preview)
                transaction.set_rollback(True)
        except (TemplateError, IntegrityError) as exc:
            raise OperationsToolError(str(exc)) from exc
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "template": preview_data,
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_MESSAGING_CONFIG_WRITE,
                ),
                "will_submit_to_meta": False,
                "next_tool": "submit_whatsapp_template",
            },
            capability=CAP_MESSAGING_CONFIG_WRITE,
            target_type="whatsapp_account",
            target_id=str(account.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={
                "operation": "create_whatsapp_template",
                "proposal_digest": _proposal_digest(proposal),
            },
        )

    try:
        template = create_template(
            organization=organization,
            account=account,
            created_by=identity.actor,
            name=values["name"],
            body=values["body"],
            category=values["category"],
            template_format=WhatsAppTemplate.Format.STANDARD,
            footer=values["footer"],
            attachment_type=WhatsAppTemplate.AttachmentType.NONE,
            buttons=values["buttons"],
            language=values["language"],
        )
    except (TemplateError, IntegrityError) as exc:
        raise OperationsToolError(str(exc)) from exc

    return ToolExecution(
        data={
            "status": "CREATED",
            "template": _template_snapshot(template),
            "submitted_to_meta": False,
            "next_tool": "submit_whatsapp_template",
            "verification": "passed",
        },
        capability=CAP_MESSAGING_CONFIG_WRITE,
        target_type="whatsapp_template",
        target_id=str(template.id),
        reason=reason,
        audit_summary={
            "operation": "create_whatsapp_template",
            "verification": "passed",
        },
    )


def submit_whatsapp_template(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_MESSAGING_CONFIG_WRITE,
        tool_name="submit_whatsapp_template",
        arguments=arguments,
    )
    template = (
        WhatsAppTemplate.objects.filter(
            pk=_uuid((arguments or {}).get("template_id"), field="template_id"),
            organization=organization,
        )
        .select_related("account")
        .first()
    )
    if template is None:
        raise OperationsToolError("WhatsApp template not found in this organization.")
    _template_account(organization=organization, account_id=str(template.account_id))

    if template.meta_template_id and template.status in {
        WhatsAppTemplate.Status.PENDING,
        WhatsAppTemplate.Status.APPROVED,
        WhatsAppTemplate.Status.PAUSED,
    }:
        return ToolExecution(
            data={
                "status": "NO_CHANGE",
                "template": _template_snapshot(template),
                "idempotent": True,
                "already_submitted_to_meta": True,
            },
            capability=CAP_MESSAGING_CONFIG_WRITE,
            target_type="whatsapp_template",
            target_id=str(template.id),
            reason=reason,
            outcome=(
                OperationsAuditEvent.Outcome.DRY_RUN
                if dry_run
                else OperationsAuditEvent.Outcome.SUCCESS
            ),
            audit_summary={
                "operation": "submit_whatsapp_template",
                "idempotent": True,
            },
        )
    if template.status != WhatsAppTemplate.Status.DRAFT:
        raise OperationsToolError(
            "Only draft WhatsApp templates can be submitted to Meta."
        )
    if template.template_format == WhatsAppTemplate.Format.CAROUSEL:
        raise OperationsToolError(
            "Carousel submission requires per-card media samples. Upload those samples "
            "through the SHVYA template UI before submission."
        )
    if template.attachment_type != WhatsAppTemplate.AttachmentType.NONE:
        metadata = state_for(template)
        if not metadata.header_sample_handle:
            raise OperationsToolError(
                "This media template needs a stored Meta header sample before submission. "
                "Upload the sample through the SHVYA template UI, then submit it here."
            )

    proposal = {
        "organization_id": str(organization.id),
        "whatsapp_account_id": str(template.account_id),
        "template_id": str(template.id),
        "name": template.name,
        "status": template.status,
        "meta_template_id": template.meta_template_id or "",
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)

    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "template": _template_snapshot(template),
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_MESSAGING_CONFIG_WRITE,
                ),
                "will_submit_to_meta": True,
                "meta_approval_required": True,
            },
            capability=CAP_MESSAGING_CONFIG_WRITE,
            target_type="whatsapp_template",
            target_id=str(template.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={
                "operation": "submit_whatsapp_template",
                "proposal_digest": _proposal_digest(proposal),
            },
        )

    current = (
        WhatsAppTemplate.objects.filter(pk=template.pk, organization=organization)
        .select_related("account")
        .first()
    )
    if current is None:
        raise OperationsApprovalRequired(
            "The template no longer exists. Run a fresh dry-run."
        )
    current_proposal = {
        "organization_id": str(organization.id),
        "whatsapp_account_id": str(current.account_id),
        "template_id": str(current.id),
        "name": current.name,
        "status": current.status,
        "meta_template_id": current.meta_template_id or "",
    }
    _ensure_approved_proposal_unchanged(arguments=arguments, proposal=current_proposal)
    try:
        current = submit_template(template=current)
    except TemplateError as exc:
        raise OperationsToolError(str(exc)) from exc

    return ToolExecution(
        data={
            "status": "SUBMITTED",
            "template": _template_snapshot(current),
            "submitted_to_meta": True,
            "meta_approval_required": current.status != WhatsAppTemplate.Status.APPROVED,
            "verification": "passed",
        },
        capability=CAP_MESSAGING_CONFIG_WRITE,
        target_type="whatsapp_template",
        target_id=str(current.id),
        reason=reason,
        audit_summary={
            "operation": "submit_whatsapp_template",
            "meta_status": current.status,
            "verification": "passed",
        },
    )



def _batch_template_state(template):
    return {
        "template_id": str(template.id),
        "name": template.name,
        "whatsapp_account_id": str(template.account_id),
        "status": template.status,
        "meta_template_id": template.meta_template_id or "",
        "template_format": template.template_format,
        "attachment_type": template.attachment_type,
    }


def _batch_template_block_reason(template):
    if template.meta_template_id and template.status in {
        WhatsAppTemplate.Status.PENDING,
        WhatsAppTemplate.Status.APPROVED,
        WhatsAppTemplate.Status.PAUSED,
    }:
        return ""
    if template.status != WhatsAppTemplate.Status.DRAFT:
        return "Only draft templates can be submitted to Meta."
    if template.template_format == WhatsAppTemplate.Format.CAROUSEL:
        return (
            "Carousel submission requires per-card media samples through the "
            "SHVYA template UI."
        )
    if template.attachment_type != WhatsAppTemplate.AttachmentType.NONE:
        metadata = state_for(template)
        if not metadata.header_sample_handle:
            return (
                "Media template requires a stored Meta header sample before submission."
            )
    return ""


def submit_whatsapp_templates(*, identity, arguments):
    """Submit up to 50 organization templates and report each Meta result."""

    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_MESSAGING_CONFIG_WRITE,
        tool_name="submit_whatsapp_templates",
        arguments=arguments,
    )
    raw_ids = (arguments or {}).get("template_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise OperationsToolError("template_ids must be a non-empty array.")
    if len(raw_ids) > 50:
        raise OperationsToolError("At most 50 WhatsApp templates can be submitted at once.")

    template_ids = []
    seen = set()
    for value in raw_ids:
        template_id = _uuid(value, field="template_ids")
        if template_id in seen:
            continue
        seen.add(template_id)
        template_ids.append(template_id)

    rows = list(
        WhatsAppTemplate.objects.filter(
            organization=organization,
            pk__in=template_ids,
        )
        .select_related("account")
    )
    by_id = {item.id: item for item in rows}
    if len(by_id) != len(template_ids):
        raise OperationsToolError(
            "One or more WhatsApp templates were not found in this organization."
        )

    ordered = [by_id[item_id] for item_id in template_ids]
    proposal_rows = []
    preview_rows = []
    for template in ordered:
        _template_account(
            organization=organization,
            account_id=str(template.account_id),
        )
        block_reason = _batch_template_block_reason(template)
        already_submitted = bool(
            template.meta_template_id
            and template.status
            in {
                WhatsAppTemplate.Status.PENDING,
                WhatsAppTemplate.Status.APPROVED,
                WhatsAppTemplate.Status.PAUSED,
            }
        )
        state = _batch_template_state(template)
        proposal_rows.append(state)
        preview_rows.append(
            {
                **state,
                "already_submitted": already_submitted,
                "can_submit": already_submitted or not block_reason,
                "block_reason": block_reason,
            }
        )

    proposal = {
        "organization_id": str(organization.id),
        "templates": proposal_rows,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)

    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "templates": preview_rows,
                "count": len(preview_rows),
                "submittable_count": sum(
                    1 for row in preview_rows if row["can_submit"]
                ),
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_MESSAGING_CONFIG_WRITE,
                ),
                "meta_approval_required": True,
            },
            capability=CAP_MESSAGING_CONFIG_WRITE,
            target_type="organization",
            target_id=str(organization.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={
                "operation": "submit_whatsapp_templates",
                "template_count": len(preview_rows),
                "proposal_digest": _proposal_digest(proposal),
            },
        )

    current_rows = list(
        WhatsAppTemplate.objects.filter(
            organization=organization,
            pk__in=template_ids,
        ).select_related("account")
    )
    current_by_id = {item.id: item for item in current_rows}
    current_proposal = {
        "organization_id": str(organization.id),
        "templates": [
            _batch_template_state(current_by_id[item_id])
            for item_id in template_ids
            if item_id in current_by_id
        ],
    }
    if len(current_proposal["templates"]) != len(template_ids):
        raise OperationsApprovalRequired(
            "One or more templates changed after review. Run a fresh dry-run."
        )
    _ensure_approved_proposal_unchanged(
        arguments=arguments,
        proposal=current_proposal,
    )

    results = []
    submitted_count = 0
    no_change_count = 0
    failed_count = 0
    for template_id in template_ids:
        template = current_by_id[template_id]
        _template_account(
            organization=organization,
            account_id=str(template.account_id),
        )
        if (
            template.meta_template_id
            and template.status
            in {
                WhatsAppTemplate.Status.PENDING,
                WhatsAppTemplate.Status.APPROVED,
                WhatsAppTemplate.Status.PAUSED,
            }
        ):
            no_change_count += 1
            results.append(
                {
                    "template_id": str(template.id),
                    "name": template.name,
                    "status": "NO_CHANGE",
                    "meta_status": template.status,
                    "meta_template_id": template.meta_template_id,
                }
            )
            continue

        block_reason = _batch_template_block_reason(template)
        if block_reason:
            failed_count += 1
            results.append(
                {
                    "template_id": str(template.id),
                    "name": template.name,
                    "status": "BLOCKED",
                    "error": block_reason,
                }
            )
            continue

        try:
            submitted = submit_template(template=template)
        except TemplateError as exc:
            failed_count += 1
            results.append(
                {
                    "template_id": str(template.id),
                    "name": template.name,
                    "status": "FAILED",
                    "error": str(exc),
                    "meta_error_code": getattr(exc, "meta_error_code", ""),
                }
            )
            continue

        submitted_count += 1
        results.append(
            {
                "template_id": str(submitted.id),
                "name": submitted.name,
                "status": "SUBMITTED",
                "meta_status": submitted.status,
                "meta_template_id": submitted.meta_template_id,
            }
        )

    overall = (
        "SUBMITTED"
        if failed_count == 0
        else "PARTIAL"
        if submitted_count or no_change_count
        else "FAILED"
    )
    return ToolExecution(
        data={
            "status": overall,
            "results": results,
            "count": len(results),
            "submitted_count": submitted_count,
            "no_change_count": no_change_count,
            "failed_count": failed_count,
            "meta_approval_required": True,
            "verification": "passed",
        },
        capability=CAP_MESSAGING_CONFIG_WRITE,
        target_type="organization",
        target_id=str(organization.id),
        reason=reason,
        audit_summary={
            "operation": "submit_whatsapp_templates",
            "template_count": len(results),
            "submitted_count": submitted_count,
            "failed_count": failed_count,
            "verification": "passed",
        },
    )
