"""Capability-discovery payload shared by MCP and the HTTP endpoint."""

from apps.integrations.operations_policy import (
    ROLE_SUPERADMIN, approval_required, effective_capabilities,
)

SUPERADMIN_ONLY_TOOLS = frozenset({
    "list_organizations", "select_organization_context", "clear_organization_context",
    "create_organization_account", "create_vault_workspace",
})


def capability_discovery(*, identity, tools):
    organization = identity.active_organization if identity.role == "SHVYA_SUPERADMIN" else identity.organization
    live = set(effective_capabilities(role=identity.role, organization=organization))
    granted = set(identity.granted_capabilities)
    effective = live & granted
    missing_oauth_grants = sorted(live - granted)
    rows = []
    for item in tools:
        name = item.get("name")
        capability = item.get("capability")
        requires_write = any(
            scope.get("type") == "oauth2" and "operations.write" in (scope.get("scopes") or [])
            for scope in (item.get("securitySchemes") or [])
        )
        allowed = (capability is None or capability in effective or name == "get_operations_context")
        allowed = allowed and (not requires_write or "operations.write" in identity.scopes)
        allowed = allowed and (name not in SUPERADMIN_ONLY_TOOLS or identity.role == ROLE_SUPERADMIN)
        rows.append({
            "name": name,
            "available": allowed,
            "capability": capability,
            "unavailable_reason": (
                "oauth_reauthorization_required"
                if capability in live and capability not in granted
                else "organization_policy_denied"
                if capability and capability not in live
                else "operations_write_scope_missing"
                if requires_write and "operations.write" not in identity.scopes
                else "superadmin_role_required"
                if name in SUPERADMIN_ONLY_TOOLS and identity.role != ROLE_SUPERADMIN
                else None
            ),
            "approval_required": bool(capability and approval_required(role=identity.role, organization=organization, capability=capability)),
            "dependencies": ["operations.read"] + (["operations.write"] if requires_write else []),
            "environment_limitations": [],
        })
    return {
        "role": identity.role,
        "organization": {"id": str(organization.id), "name": organization.name, "active": organization.is_active} if organization else None,
        "oauth_scopes": sorted(identity.scopes),
        "granted_capabilities": sorted(granted),
        "effective_capabilities": sorted(effective),
        "missing_oauth_grants": missing_oauth_grants,
        "oauth_reauthorization_required": bool(missing_oauth_grants),
        "oauth_reauthorization_instructions": (
            "Re-authorize the existing SHVYA MCP connection through OAuth to approve newly enabled permissions. "
            "If the client has no re-authorize action, disconnect and reconnect. "
            "Refreshing the token alone intentionally does not expand the existing grant."
            if missing_oauth_grants else None
        ),
        "tools": rows,
        "unsupported_features": [
            {"name": "Voice Agent", "status": "excluded_from_scope", "reason": "Handled separately."},
        ],
        "global_dependencies": [
            "get_operations_context before organization work",
            "explicit active organization context",
            "tenant-scoped capability and OAuth grant",
            "schema validation before handler execution",
            "dry-run and approval for production mutations",
            "immutable audit event and readback for mutations",
        ],
        "environment_limitations": [
            "Provider credentials, tokens and secrets are never returned.",
            "Acceptance simulations do not send messages or activate automation.",
            "AI flow tests use isolated disposable records and production reply logic; outbound transport remains blocked.",
            "AI flow tests can consume the approved AI provider budget; delivery is not verified by a flow test.",
            "Hosted WhatsApp group sends require approval of each exact sender, group and message.",
        ],
    }
