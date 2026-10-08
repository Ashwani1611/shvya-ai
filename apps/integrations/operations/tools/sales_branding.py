"""Tenant-scoped SHVYA Sales template metadata/branding MCP tools."""
from urllib.parse import urlparse
from django.core.exceptions import ValidationError
from django.db import transaction
from apps.sales.models import SalesTemplate
from apps.integrations.operations_models import OperationsAuditEvent
from apps.integrations.operations_policy import CAP_ORGANIZATION_READ, CAP_SALES_TEMPLATE_WRITE, approval_required
from apps.integrations.operations_tools import (
    OperationsToolError, ToolExecution, _organization_for,
    _require_operations_capability, _write_gate, _uuid,
    _ensure_approved_proposal_unchanged, _proposal_digest,
)

ALLOWED = frozenset({"logo_url", "signature_url", "accent_color", "header_text", "footer_text"})


def _snapshot(item):
    return {
        "id": str(item.pk), "name": item.name, "document_type": item.document_type,
        "is_active": item.is_active, "is_default": item.is_default,
        "logo_url": item.logo_url, "signature_url": item.signature_url,
        "has_logo_file": bool(item.logo_file), "has_signature_file": bool(item.signature_file),
        "accent_color": item.accent_color, "header_text": item.header_text,
        "footer_text": item.footer_text,
    }


def list_sales_templates(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(identity=identity, organization=organization, capability=CAP_ORGANIZATION_READ)
    templates = [_snapshot(item) for item in SalesTemplate.objects.filter(organization=organization).order_by("document_type", "name")[:100]]
    return ToolExecution(data={"templates": templates, "count": len(templates)}, capability=CAP_ORGANIZATION_READ, target_type="organization", target_id=str(organization.pk))


def _changes(raw):
    if not isinstance(raw, dict) or not raw or set(raw) - ALLOWED:
        raise OperationsToolError("changes must contain only supported Sales branding fields.")
    values = {}
    for key, value in raw.items():
        if not isinstance(value, str):
            raise OperationsToolError(f"{key} must be a string.")
        value = value.strip()
        if key in ("logo_url", "signature_url") and value:
            parsed = urlparse(value)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                raise OperationsToolError(f"{key} requires a public HTTPS asset URL without credentials or fragments.")
            if parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"} or parsed.hostname.lower().endswith((".local", ".internal")):
                raise OperationsToolError(f"{key} must not point to a private host.")
        if key == "accent_color" and (len(value) != 7 or value[0] != "#" or any(ch not in "0123456789abcdefABCDEF" for ch in value[1:])):
            raise OperationsToolError("accent_color must be a six-digit hex color.")
        if len(value) > (2048 if key.endswith("_url") else 255 if key == "header_text" else 4000 if key == "footer_text" else 7):
            raise OperationsToolError(f"{key} is too long.")
        values[key] = value
    return values


def upsert_sales_template_branding(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(identity=identity, organization=organization,
        capability=CAP_SALES_TEMPLATE_WRITE, tool_name="upsert_sales_template_branding", arguments=arguments)
    pk = _uuid((arguments or {}).get("template_id"), field="template_id")
    template = SalesTemplate.objects.filter(pk=pk, organization=organization).first()
    if template is None:
        raise OperationsToolError("Sales template not found in this organization.")
    changes = _changes((arguments or {}).get("changes"))
    before = _snapshot(template)
    after = dict(before, **changes)
    proposal = {"template_id": str(pk), "before": before, "after": after}
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return ToolExecution(data={"status": "DRY_RUN", "before": before, "after": after,
            "approval_required": approval_required(role=identity.role, organization=organization, capability=CAP_SALES_TEMPLATE_WRITE),
            "documents_sent": 0}, capability=CAP_SALES_TEMPLATE_WRITE, target_type="sales_template",
            target_id=str(pk), reason=reason, outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={"proposal_digest": _proposal_digest(proposal)})
    with transaction.atomic():
        locked = SalesTemplate.objects.select_for_update().filter(pk=pk, organization=organization).first()
        if locked is None:
            raise OperationsToolError("Sales template was removed after preview.")
        locked_before = _snapshot(locked)
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal={"template_id": str(pk), "before": locked_before, "after": dict(locked_before, **changes)})
        for key, value in changes.items():
            setattr(locked, key, value)
        try:
            locked.full_clean()
        except ValidationError as exc:
            raise OperationsToolError(str(exc)) from exc
        locked.save(update_fields=[*changes.keys(), "updated_at"])
        result = _snapshot(locked)
    return ToolExecution(data={"status": "UPDATED", "template": result, "verification": "passed", "documents_sent": 0},
        capability=CAP_SALES_TEMPLATE_WRITE, target_type="sales_template", target_id=str(pk),
        reason=reason, audit_summary={"verification": "passed"})
