"""Capability and approval policy for the SHVYA Operations MCP."""

from __future__ import annotations

from apps.integrations.operations_models import OperationsPolicy


ROLE_SUPERADMIN = "SHVYA_SUPERADMIN"
ROLE_ORGANIZATION_ADMIN = "ORGANIZATION_ADMIN"

CAP_ORGANIZATION_READ = "organization.read"
CAP_DIAGNOSTICS_READ = "diagnostics.read"
CAP_AUDIT_READ = "audit.read"
CAP_LEAD_STAGE_WRITE = "lead.stage.write"
CAP_LEAD_ATTRIBUTES_WRITE = "lead.attributes.write"
CAP_AI_CONFIG_WRITE = "ai.config.write"
CAP_CRM_CONFIG_WRITE = "crm.config.write"
CAP_AUTOMATION_CONFIG_WRITE = "automation.config.write"

READ_CAPABILITIES = (
    CAP_ORGANIZATION_READ,
    CAP_DIAGNOSTICS_READ,
    CAP_AUDIT_READ,
)
WRITE_CAPABILITIES = (
    CAP_LEAD_STAGE_WRITE,
    CAP_LEAD_ATTRIBUTES_WRITE,
    CAP_AI_CONFIG_WRITE,
    CAP_CRM_CONFIG_WRITE,
    CAP_AUTOMATION_CONFIG_WRITE,
)
ALL_CAPABILITIES = READ_CAPABILITIES + WRITE_CAPABILITIES

DEFAULT_ORG_CAPABILITIES = list(READ_CAPABILITIES)
DEFAULT_APPROVAL_REQUIRED = list(WRITE_CAPABILITIES)

CAPABILITY_LABELS = {
    CAP_ORGANIZATION_READ: "Read business & CRM configuration",
    CAP_DIAGNOSTICS_READ: "Run diagnostics and traces",
    CAP_AUDIT_READ: "Read Operations AI audit history",
    CAP_LEAD_STAGE_WRITE: "Move leads between active stages/pipelines",
    CAP_LEAD_ATTRIBUTES_WRITE: "Update non-sensitive lead attributes",
    CAP_AI_CONFIG_WRITE: "Update organization AI profile / Playbook",
    CAP_CRM_CONFIG_WRITE: "Configure CRM pipelines, stages and attributes",
    CAP_AUTOMATION_CONFIG_WRITE: "Configure Workflows and Cadence",
}


class OperationsPolicyError(PermissionError):
    pass


def policy_for(organization):
    policy, _ = OperationsPolicy.objects.get_or_create(
        organization=organization,
        defaults={
            "allowed_capabilities": DEFAULT_ORG_CAPABILITIES,
            "approval_required_capabilities": DEFAULT_APPROVAL_REQUIRED,
        },
    )
    return policy


def effective_capabilities(*, role, organization=None):
    if role == ROLE_SUPERADMIN:
        return set(ALL_CAPABILITIES)
    if role != ROLE_ORGANIZATION_ADMIN or organization is None:
        return set()

    policy = policy_for(organization)
    if not policy.organization_admin_enabled:
        return set()

    allowed = {
        str(item)
        for item in (policy.allowed_capabilities or [])
        if str(item) in ALL_CAPABILITIES
    }
    return allowed


def approval_required(*, role, organization, capability):
    # Superadmin is powerful, but customer-state mutations still require the
    # agent to present an explicit approved=True flag after its dry-run.
    if capability in WRITE_CAPABILITIES and role == ROLE_SUPERADMIN:
        return True
    if role != ROLE_ORGANIZATION_ADMIN or organization is None:
        return False
    policy = policy_for(organization)
    values = {str(item) for item in (policy.approval_required_capabilities or [])}
    return capability in values


def require_capability(*, role, organization, capability):
    if capability not in effective_capabilities(role=role, organization=organization):
        raise OperationsPolicyError(
            f"Capability '{capability}' is not enabled for this Operations MCP session."
        )
