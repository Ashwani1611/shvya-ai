"""Capability and approval policy for the SHVYA Operations MCP."""

from __future__ import annotations

from apps.integrations.operations_models import OperationsPolicy


ROLE_SUPERADMIN = "SHVYA_SUPERADMIN"
ROLE_ORGANIZATION_ADMIN = "ORGANIZATION_ADMIN"

CAP_ORGANIZATION_READ = "organization.read"
CAP_DIAGNOSTICS_READ = "diagnostics.read"
CAP_AUDIT_READ = "audit.read"
CAP_SETUP_LIBRARY_READ = "setup.library.read"
CAP_SETUP_ARTIFACTS_PREPARE = "setup.artifacts.prepare"
CAP_SETUP_INTAKE_READ = "setup.intake.read"
CAP_SETUP_INTAKE_WRITE = "setup.intake.write"
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
CAP_CONFIGURATION_PLAN_WRITE = "configuration.plan.write"
CAP_TRACE_CONTENT_READ = "trace.content.read"
CAP_CALENDAR_CONFIG_WRITE = "calendar.config.write"
CAP_INTEGRATION_LIFECYCLE_WRITE = "integration.lifecycle.write"
CAP_OPERATIONS_TASK_WRITE = "operations.task.write"
CAP_TEAM_SETTINGS_WRITE = "team.settings.write"
CAP_ORGANIZATION_CREATE = "organization.create"
CAP_LEAD_READ = "lead.read"
CAP_LEAD_CREATE = "lead.create"
CAP_LEAD_WRITE = "lead.write"
CAP_LEAD_IMPORT = "lead.import"
CAP_VAULT_READ = "vault.read"
CAP_VAULT_WRITE = "vault.write"
CAP_CHANNEL_GROUP_READ = "channel.group.read"
CAP_CHANNEL_GROUP_SEND = "channel.group.send"
CAP_AI_FLOW_TEST_WRITE = "ai.flow_testing.write"

# Platform provisioning can never be delegated by a tenant policy. Group sends
# always require approval of the exact sender, group and message, even when an
# organization allows routine configuration edits without a second approval.
SUPERADMIN_ONLY_CAPABILITIES = frozenset({CAP_ORGANIZATION_CREATE})
ALWAYS_APPROVAL_CAPABILITIES = frozenset({CAP_CHANNEL_GROUP_SEND, CAP_AI_FLOW_TEST_WRITE})

READ_CAPABILITIES = (
    CAP_ORGANIZATION_READ,
    CAP_DIAGNOSTICS_READ,
    CAP_AUDIT_READ,
    CAP_SETUP_LIBRARY_READ,
    CAP_SETUP_ARTIFACTS_PREPARE,
    CAP_SETUP_INTAKE_READ,
    CAP_TRACE_CONTENT_READ,
    CAP_LEAD_READ,
    CAP_VAULT_READ,
    CAP_CHANNEL_GROUP_READ,
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
    CAP_CONFIGURATION_PLAN_WRITE,
    CAP_SETUP_INTAKE_WRITE,
    CAP_CALENDAR_CONFIG_WRITE,
    CAP_INTEGRATION_LIFECYCLE_WRITE,
    CAP_OPERATIONS_TASK_WRITE,
    CAP_TEAM_SETTINGS_WRITE,
    CAP_ORGANIZATION_CREATE,
    CAP_LEAD_CREATE,
    CAP_LEAD_WRITE,
    CAP_LEAD_IMPORT,
    CAP_VAULT_WRITE,
    CAP_CHANNEL_GROUP_SEND,
    CAP_AI_FLOW_TEST_WRITE,
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

# New setup permissions must be explicitly enabled and consented to. Keeping
# these defaults explicit prevents future read capabilities expanding access.
DEFAULT_ORG_CAPABILITIES = [
    CAP_ORGANIZATION_READ,
    CAP_DIAGNOSTICS_READ,
    CAP_AUDIT_READ,
]
DEFAULT_APPROVAL_REQUIRED = list(WRITE_CAPABILITIES)

CAPABILITY_LABELS = {
    CAP_ORGANIZATION_READ: "Read business & CRM configuration",
    CAP_DIAGNOSTICS_READ: "Run diagnostics and traces",
    CAP_AUDIT_READ: "Read Operations AI audit history",
    CAP_SETUP_LIBRARY_READ: "Read SHVYA setup skills, prompts and templates",
    CAP_SETUP_ARTIFACTS_PREPARE: "Prepare setup drafts and analyze supplied group exports",
    CAP_SETUP_INTAKE_READ: "Read company setup intake and source history",
    CAP_SETUP_INTAKE_WRITE: "Create, update and archive company setup intake",
    CAP_LEAD_STAGE_WRITE: "Move leads between active stages/pipelines",
    CAP_LEAD_ATTRIBUTES_WRITE: "Update non-sensitive lead attributes",
    CAP_AI_CONFIG_WRITE: "Update organization AI profile / Playbook",
    CAP_PIPELINE_CONFIG_WRITE: "Configure CRM pipelines",
    CAP_STAGE_CONFIG_WRITE: "Configure CRM stages",
    CAP_ATTRIBUTE_CONFIG_WRITE: "Configure CRM attributes",
    CAP_WORKFLOW_CONFIG_WRITE: "Configure Workflows",
    CAP_CADENCE_CONFIG_WRITE: "Configure Cadence",
    CAP_MESSAGING_CONFIG_WRITE: "Configure messaging automation settings",
    CAP_CONFIGURATION_PLAN_WRITE: "Create/apply/rollback organization configuration plans",
    CAP_TRACE_CONTENT_READ: "Read bounded rendered AI trace content",
    CAP_CALENDAR_CONFIG_WRITE: "Configure Calendar and booking reminders",
    CAP_INTEGRATION_LIFECYCLE_WRITE: "Connect, reconnect, test and disconnect integrations",
    CAP_OPERATIONS_TASK_WRITE: "Track onboarding, integration and audit commitments",
    CAP_TEAM_SETTINGS_WRITE: "Configure team responder, ownership, handoff, sender and Co-Pilot settings",
    CAP_ORGANIZATION_CREATE: "Create customer organizations (SHVYA Superadmin only)",
    CAP_LEAD_READ: "Read CRM leads and their business details",
    CAP_LEAD_CREATE: "Create CRM leads without sending welcome messages",
    CAP_LEAD_WRITE: "Update CRM lead details",
    CAP_LEAD_IMPORT: "Import reviewed batches of CRM leads",
    CAP_VAULT_READ: "Read the organization's private SHVYA Vault",
    CAP_VAULT_WRITE: "Configure the SHVYA Vault and add client-visible material",
    CAP_CHANNEL_GROUP_READ: "Read hosted WhatsApp support groups",
    CAP_CHANNEL_GROUP_SEND: "Send specifically approved hosted WhatsApp group messages",
    CAP_AI_FLOW_TEST_WRITE: "Run isolated AI conversation tests with approved provider budgets",
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
    ) - SUPERADMIN_ONLY_CAPABILITIES


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
    if capability in ALWAYS_APPROVAL_CAPABILITIES:
        return True
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
