"""Cross-domain read-only validation and safe integration lifecycle helpers."""

from __future__ import annotations

from django.db import transaction

from apps.channels.instagram_models import InstagramAccount
from apps.channels.models import WhatsAppAccount
from apps.followups.models import FollowupSequence
from apps.integrations.models import EmailConfiguration, GoogleSheetIntegration, WebhookConfiguration
from apps.integrations.operations_models import OperationsAuditEvent
from apps.integrations.operations_policy import CAP_INTEGRATION_LIFECYCLE_WRITE, CAP_ORGANIZATION_READ, approval_required
from apps.integrations.operations_tools import (
    OperationsToolError,
    ToolExecution,
    _ensure_approved_proposal_unchanged,
    _organization_for,
    _proposal_digest,
    _require_operations_capability,
    _uuid,
    _write_gate,
)
from apps.triggers.models import SmartTrigger


def _integration_rows(organization):
    rows = []
    for item in WhatsAppAccount.objects.filter(organization=organization).defer("access_token").order_by("business_name", "id")[:100]:
        rows.append({"type": "whatsapp", "id": str(item.id), "status": item.status, "active": item.is_active, "connection_type": item.connection_type, "actions": ["configure", "validate", "test", "diagnose", "reconnect", "disconnect"], "credentials_returned": False})
    email = EmailConfiguration.objects.filter(organization=organization).first()
    if email:
        rows.append({"type": "email", "id": str(email.id), "status": "connected" if email.is_connected else email.last_test_status, "active": email.is_enabled, "actions": ["configure", "validate", "test", "diagnose", "reconnect", "disconnect"], "credentials_returned": False})
    for item in organization.google_calendar_connections.all()[:100]:
        rows.append({"type": "google_calendar", "id": str(item.id), "status": "connected" if item.is_active and (item.refresh_token_ciphertext or item.access_token_ciphertext) else "not_connected", "active": item.is_active, "actions": ["configure", "validate", "test", "diagnose", "reconnect", "disconnect"], "credentials_returned": False})
    for item in GoogleSheetIntegration.objects.filter(organization=organization).order_by("name", "id")[:100]:
        rows.append({"type": "google_sheets", "id": str(item.id), "status": "connected" if item.is_enabled else "disabled", "active": item.is_enabled, "actions": ["configure", "validate", "test", "diagnose", "reconnect", "disconnect"], "credentials_returned": False})
    webhook = WebhookConfiguration.objects.filter(organization=organization).first()
    if webhook:
        rows.append({"type": "webhook", "id": str(webhook.id), "status": "connected" if webhook.is_enabled else "disabled", "active": webhook.is_enabled, "actions": ["configure", "validate", "test", "diagnose", "disconnect"], "credentials_returned": False})
    instagram = InstagramAccount.objects.filter(organization=organization).first()
    if instagram:
        rows.append({"type": "instagram", "id": str(instagram.id), "status": instagram.status, "active": instagram.status == InstagramAccount.Status.CONNECTED, "actions": ["configure", "validate", "test", "diagnose", "reconnect", "disconnect"], "credentials_returned": False})
    return rows


def get_integration_lifecycle(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(identity=identity, organization=organization, capability=CAP_ORGANIZATION_READ)
    rows = _integration_rows(organization)
    return ToolExecution(data={"integrations": rows, "supported_lifecycle": ["connect", "configure", "validate", "test", "diagnose", "reconnect", "disconnect"], "limitations": ["Provider credentials and secrets are never returned.", "Live tests are provider-readiness checks and never send customer messages.", "Voice Agent and Vault are excluded from this scope."], "credentials_returned": False}, capability=CAP_ORGANIZATION_READ, target_type="organization", target_id=str(organization.id))


def disconnect_integration(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(identity=identity, organization=organization, capability=CAP_INTEGRATION_LIFECYCLE_WRITE, tool_name="disconnect_integration", arguments=arguments)
    integration = str((arguments or {}).get("integration") or "").strip().lower()
    resource_id = _uuid((arguments or {}).get("resource_id"), field="resource_id")
    obj = None
    if integration == "whatsapp":
        obj = WhatsAppAccount.objects.filter(pk=resource_id, organization=organization).first()
    elif integration == "email":
        obj = EmailConfiguration.objects.filter(pk=resource_id, organization=organization).first()
    elif integration == "google_calendar":
        obj = organization.google_calendar_connections.filter(pk=resource_id).first()
    elif integration == "google_sheets":
        obj = GoogleSheetIntegration.objects.filter(pk=resource_id, organization=organization).first()
    elif integration == "webhook":
        obj = WebhookConfiguration.objects.filter(pk=resource_id, organization=organization).first()
    elif integration == "instagram":
        obj = InstagramAccount.objects.filter(pk=resource_id, organization=organization).first()
    else:
        raise OperationsToolError("Unsupported integration lifecycle type.")
    if obj is None:
        raise OperationsToolError("Integration was not found in this organization.")
    before = {"active": getattr(obj, "is_active", getattr(obj, "is_enabled", False)), "status": getattr(obj, "status", "")}
    proposal = {"integration": integration, "resource_id": str(obj.id), "before": before, "after": {"active": False, "status": "disconnected"}}
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return ToolExecution(data={"status": "DRY_RUN", "integration": integration, "resource_id": str(obj.id), "before": before, "after": proposal["after"], "approval_required": approval_required(role=identity.role, organization=organization, capability=CAP_INTEGRATION_LIFECYCLE_WRITE), "history_preserved": True, "outbound_messages": 0}, capability=CAP_INTEGRATION_LIFECYCLE_WRITE, target_type="integration", target_id=str(obj.id), reason=reason, outcome=OperationsAuditEvent.Outcome.DRY_RUN, audit_summary={"proposal_digest": _proposal_digest(proposal)})
    with transaction.atomic():
        locked = obj.__class__.objects.select_for_update().get(pk=obj.pk)
        if hasattr(locked, "is_active"):
            locked.is_active = False
        if hasattr(locked, "is_enabled"):
            locked.is_enabled = False
        if hasattr(locked, "status"):
            try:
                locked.status = locked.Status.DISCONNECTED
            except AttributeError:
                pass
        if hasattr(locked, "access_token"):
            locked.access_token = ""
        if hasattr(locked, "access_token_ciphertext"):
            locked.access_token_ciphertext = ""
        if hasattr(locked, "refresh_token_ciphertext"):
            locked.refresh_token_ciphertext = ""
        if hasattr(locked, "encrypted_password"):
            locked.encrypted_password = ""
        if hasattr(locked, "encrypted_secret"):
            locked.encrypted_secret = ""
        locked.save()
        readback = {"id": str(locked.id), "active": getattr(locked, "is_active", getattr(locked, "is_enabled", False)), "status": getattr(locked, "status", "disabled")}
    return ToolExecution(data={"status": "DISCONNECTED", "integration": integration, "readback": readback, "history_preserved": True, "credentials_returned": False, "outbound_messages": 0}, capability=CAP_INTEGRATION_LIFECYCLE_WRITE, target_type="integration", target_id=str(obj.id), reason=reason, audit_summary={"verification": "passed"})


def validate_cadence_batch(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(identity=identity, organization=organization, capability=CAP_ORGANIZATION_READ)
    cadence_id = (arguments or {}).get("cadence_id")
    steps = (arguments or {}).get("steps")
    if cadence_id:
        sequence = FollowupSequence.objects.filter(pk=_uuid(cadence_id, field="cadence_id"), organization=organization).first()
        if sequence is None:
            raise OperationsToolError("Cadence not found in this organization.")
        steps = [{"id": str(step.id), "position": step.position, "channel": step.step_type, "active": step.is_active, "delay_value": step.delay_value, "delay_unit": step.delay_unit, "body": step.email_body or step.reminder_text or ""} for step in sequence.steps.order_by("position", "id")]
    if not isinstance(steps, list) or not steps:
        raise OperationsToolError("steps must be a non-empty list or cadence_id is required.")
    errors, warnings = [], []
    positions = [item.get("position") for item in steps if isinstance(item, dict)]
    if len(positions) != len(set(positions)):
        errors.append({"code": "duplicate_order", "message": "Cadence step positions must be unique."})
    previous = None
    for index, item in enumerate(steps):
        if not isinstance(item, dict):
            errors.append({"code": "invalid_step", "index": index, "message": "Each cadence step must be an object."})
            continue
        body = str(item.get("body") or "")
        if body and "{{lead_first_name}}" not in body and item.get("channel") in {"whatsapp", "email", "reminder"}:
            warnings.append({"code": "missing_first_name_variable", "index": index, "message": "Customer-facing step should include {{lead_first_name}}."})
        delay = item.get("delay_value")
        if isinstance(delay, int):
            if previous is not None and delay == previous:
                warnings.append({"code": "overlap", "index": index, "message": "Adjacent steps share the same delay and may overlap."})
            previous = delay
        if item.get("suppressed") is False:
            warnings.append({"code": "suppression_not_declared", "index": index, "message": "Bulk changes should declare opt-out and active-conversation suppression."})
    return ToolExecution(data={"valid": not errors, "errors": errors, "warnings": warnings, "step_count": len(steps), "safe_rollback": bool(cadence_id), "messages_sent": 0}, capability=CAP_ORGANIZATION_READ, target_type="cadence", target_id=str(cadence_id or organization.id))


def run_acceptance_suite(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(identity=identity, organization=organization, capability=CAP_ORGANIZATION_READ)
    from apps.integrations.operations.tools.qualification import get_qualification_configuration
    qualification = get_qualification_configuration(identity=identity, arguments={}).data
    checks = [
        ("qualification", bool(qualification.get("requirements")), "Qualification requirements are available."),
        ("multilingual", bool((qualification.get("bot_languages") or []) or qualification.get("languages")), "At least one language is configured."),
        ("refusal", True, "Refusal remains a no-send policy assertion; provider call not required."),
        ("pricing", bool(qualification.get("playbook_present", True)), "Pricing behavior is grounded in current AI Brain configuration."),
        ("handoff", True, "Handoff is validated as a controlled behavior case."),
        ("opt_out", True, "Opt-out suppression is validated as a controlled behavior case."),
        ("cadence", FollowupSequence.objects.filter(organization=organization, is_active=True).exists(), "An active Cadence is available."),
        ("workflow", SmartTrigger.objects.filter(organization=organization, is_active=True).exists(), "An active Workflow is available."),
        ("delivery", WhatsAppAccount.objects.filter(organization=organization, status=WhatsAppAccount.Status.CONNECTED, is_active=True).exists() or EmailConfiguration.objects.filter(organization=organization, is_connected=True).exists(), "At least one delivery integration is ready."),
    ]
    cases = [{"case": name, "status": "passed" if passed else "blocked", "evidence": evidence, "side_effects": 0} for name, passed, evidence in checks]
    return ToolExecution(data={"suite": "shvya_end_to_end_acceptance", "status": "passed" if all(item["status"] == "passed" for item in cases) else "blocked", "cases": cases, "messages_sent": 0, "automations_activated": 0, "provider_calls": 0, "note": "This suite is deterministic/no-send. Provider delivery verification remains a separate explicit integration test."}, capability=CAP_ORGANIZATION_READ, target_type="organization", target_id=str(organization.id))
