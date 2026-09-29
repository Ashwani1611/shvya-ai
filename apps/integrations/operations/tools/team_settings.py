"""Tenant-scoped team and user-level operating controls."""

from copy import deepcopy

from django.db import transaction

from apps.accounts.models import User
from apps.crm.models import Pipeline
from apps.integrations.operations_models import OperationsAuditEvent
from apps.integrations.operations_policy import CAP_ORGANIZATION_READ, CAP_TEAM_SETTINGS_WRITE, approval_required
from apps.integrations.operations_tools import ToolExecution, OperationsToolError, _organization_for, _require_operations_capability, _write_gate, _ensure_approved_proposal_unchanged, _proposal_digest, _reject_secret_like_content
from services.copilot_service import get_copilot_config, update_copilot_config


SUPPORTED_KEYS = {"responder_hours", "handoff_rules", "sender_identity", "ai_ownership", "copilot"}


def _snapshot(organization):
    settings = organization.settings if isinstance(organization.settings, dict) else {}
    return {
        "responder_hours": deepcopy(settings.get("responder_hours") or {}),
        "handoff_rules": deepcopy(settings.get("handoff_rules") or {}),
        "sender_identity": deepcopy(settings.get("sender_identity") or {}),
        "ai_ownership": deepcopy(settings.get("ai_ownership") or {}),
        "copilot": get_copilot_config(organization),
        "pipelines": [{"id": str(item.id), "name": item.name, "owner_id": str(item.owner_id) if item.owner_id else None, "ai_enabled": item.ai_enabled} for item in Pipeline.objects.filter(organization=organization, is_active=True).order_by("name", "id")[:100]],
        "users": [{"id": str(item.id), "name": item.name, "role": item.role, "active": item.is_active} for item in User.objects.filter(organization=organization, is_active=True).order_by("name", "id")[:100]],
    }


def get_team_settings(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(identity=identity, organization=organization, capability=CAP_ORGANIZATION_READ)
    return ToolExecution(data={"settings": _snapshot(organization), "credentials_returned": False}, capability=CAP_ORGANIZATION_READ, target_type="organization", target_id=str(organization.id))


def upsert_team_settings(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(identity=identity, organization=organization, capability=CAP_TEAM_SETTINGS_WRITE, tool_name="upsert_team_settings", arguments=arguments)
    changes = (arguments or {}).get("changes")
    if not isinstance(changes, dict) or not changes or set(changes) - SUPPORTED_KEYS:
        raise OperationsToolError("changes must contain only supported team setting groups.")
    for key, value in changes.items():
        if not isinstance(value, dict):
            raise OperationsToolError(f"{key} must be an object.")
        _reject_secret_like_content(value, field=f"team_settings.{key}")
    before = _snapshot(organization)
    after = deepcopy(before)
    after.update(changes)
    proposal = {"organization_id": str(organization.id), "before": before, "after": after}
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return ToolExecution(data={"status": "DRY_RUN", "before": before, "after": after, "approval_required": approval_required(role=identity.role, organization=organization, capability=CAP_TEAM_SETTINGS_WRITE), "messages_sent": 0, "automation_activated": False}, capability=CAP_TEAM_SETTINGS_WRITE, target_type="organization", target_id=str(organization.id), reason=reason, outcome=OperationsAuditEvent.Outcome.DRY_RUN, audit_summary={"proposal_digest": _proposal_digest(proposal)})
    with transaction.atomic():
        locked = organization.__class__.objects.select_for_update().get(pk=organization.pk)
        settings = dict(locked.settings or {})
        copilot_changes = changes.get("copilot")
        if copilot_changes:
            update_copilot_config(locked, copilot_changes)
            locked.refresh_from_db()
            settings = dict(locked.settings or {})
        for key in SUPPORTED_KEYS - {"copilot"}:
            if key in changes:
                settings[key] = changes[key]
        locked.settings = settings
        locked.save(update_fields=["settings", "updated_at"])
        readback = _snapshot(locked)
    return ToolExecution(data={"status": "UPDATED", "settings": readback, "verification": "passed", "messages_sent": 0, "automation_activated": False}, capability=CAP_TEAM_SETTINGS_WRITE, target_type="organization", target_id=str(organization.id), reason=reason, audit_summary={"verification": "passed"})
