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
# Legacy umbrella values remain recognized for backward-compatible stored
# policies, but the Superadmin UI and live tool policy use granular controls.
CAP_CRM_CONFIG_WRITE = "crm.config.write"
CAP_AUTOMATION_CONFIG_WRITE = "automation.config.write"

CAP_PIPELINE_CONFIG_WRITE = "crm.pipeline.config.write"
CAP_STAGE_CONFIG_WRITE = "crm.stage.config.write"
CAP_ATTRIBUTE_CONFIG_WRITE = "crm.attribute.config.write"
CAP_WORKFLOW_CONFIG_WRITE = "automation.workflow.config.write"
CAP_CADENCE_CONFIG_WRITE = "automation.cadence.config.write"
CAP_MESSAGING_CONFIG_WRITE = "automation.messaging.config.write"

READ_CAPABILITIES = (
    CAP_ORGANIZATION_READ,
    CAP_DIAGNOSTICS_READ,
    CAP_AUDIT_READ,
)
WRITE_CAPABILITIES = (
    CAP_LEAD_STAGE_WRITE,
    CAP_LEAD_ATTRIBUTES_WRITE,
    CAP_AI_CONFIG_WRITE,
    CAP_PIPELINE_CONFIG_WRITE,
    CAP_STAGE_CONFIG_WRITE,
    CAP_ATTRIBUTE_CONFIG_WRITE,
    CAP_WORKFLOW_CONFIG_WRITE,
    CAP_CADENCE_CONFIG_WRITE,
    CAP_MESSAGING_CONFIG_WRITE,
)
ALL_CAPABILITIES = READ_CAPABILITIES + WRITE_CAPABILITIES

LEGACY_CAPABILITY_EXPANSIONS = {
    CAP_CRM_CONFIG_WRITE: (
        CAP_PIPELINE_CONFIG_WRITE,
        CAP_STAGE_CONFIG_WRITE,
        CAP_ATTRIBUTE_CONFIG_WRITE,
    ),
    CAP_AUTOMATION_CONFIG_WRITE: (
        CAP_WORKFLOW_CONFIG_WRITE,
        CAP_CADENCE_CONFIG_WRITE,
        CAP_MESSAGING_CONFIG_WRITE,
    ),
}

DEFAULT_ORG_CAPABILITIES = list(READ_CAPABILITIES)
DEFAULT_APPROVAL_REQUIRED = list(WRITE_CAPABILITIES)

CAPABILITY_LABELS = {
    CAP_ORGANIZATION_READ: "Read business & CRM configuration",
    CAP_DIAGNOSTICS_READ: "Run diagnostics and traces",
    CAP_AUDIT_READ: "Read Operations AI audit history",
    CAP_LEAD_STAGE_WRITE: "Move leads between active stages/pipelines",
    CAP_LEAD_ATTRIBUTES_WRITE: "Update non-sensitive lead attributes",
    CAP_AI_CONFIG_WRITE: "Update organization AI profile / Playbook",
    CAP_PIPELINE_CONFIG_WRITE: "Configure CRM pipelines",
    CAP_STAGE_CONFIG_WRITE: "Configure CRM stages",
    CAP_ATTRIBUTE_CONFIG_WRITE: "Configure CRM attributes",
    CAP_WORKFLOW_CONFIG_WRITE: "Configure Workflows",
    CAP_CADENCE_CONFIG_WRITE: "Configure Cadence",
    CAP_MESSAGING_CONFIG_WRITE: "Configure messaging automation settings",
}


class OperationsPolicyError(PermissionError):
    pass


def policy_for(organization):
    policy = OperationsPolicy.objects.filter(organization=organization).first()
    if policy is not None:
        return policy
    # Read paths must stay read-only. Return an unsaved disabled default until
    # SHVYA Superadmin explicitly persists a policy for this organization.
    return OperationsPolicy(
        organization=organization,
        organization_admin_enabled=False,
        allowed_capabilities=list(DEFAULT_ORG_CAPABILITIES),
        approval_required_capabilities=list(DEFAULT_APPROVAL_REQUIRED),
    )


def expand_capabilities(values):
    raw_values = {
        str(item)
        for item in (values or [])
    }
    expanded_values = {
        item
        for item in raw_values
        if item in ALL_CAPABILITIES
    }
    for legacy_capability, expanded in LEGACY_CAPABILITY_EXPANSIONS.items():
        if legacy_capability in raw_values:
            expanded_values.update(expanded)
    return expanded_values


def effective_capabilities(*, role, organization=None):
    if role == ROLE_SUPERADMIN:
        return set(ALL_CAPABILITIES)
    if role != ROLE_ORGANIZATION_ADMIN or organization is None:
        return set()

    policy = policy_for(organization)
    if not policy.organization_admin_enabled:
        return set()

    return expand_capabilities(
        policy.allowed_capabilities
    )


def capabilities_for_grant(
    *,
    role,
    organization=None,
    allow_writes=False,
):
    capabilities = set(
        effective_capabilities(
            role=role,
            organization=organization,
        )
    )
    if not allow_writes:
        capabilities.difference_update(
            WRITE_CAPABILITIES
        )
    return capabilities


def approval_required(*, role, organization, capability):
    # Superadmin is powerful, but customer-state mutations still require the
    # agent to present an explicit approved=True flag after its dry-run.
    if capability in WRITE_CAPABILITIES and role == ROLE_SUPERADMIN:
        return True
    if role != ROLE_ORGANIZATION_ADMIN or organization is None:
        return False
    policy = policy_for(organization)
    values = {
        str(item)
        for item in (
            policy.approval_required_capabilities
            or []
        )
    }
    if capability in values:
        return True
    return any(
        legacy_capability in values
        and capability in expanded
        for legacy_capability, expanded
        in LEGACY_CAPABILITY_EXPANSIONS.items()
    )


def require_capability(*, role, organization, capability):
    if capability not in effective_capabilities(role=role, organization=organization):
        raise OperationsPolicyError(
            f"Capability '{capability}' is not enabled for this Operations MCP session."
        )
