"""Capability-discovery payload shared by MCP and the HTTP endpoint."""

from apps.integrations.operations_policy import approval_required, effective_capabilities


def capability_discovery(*, identity, tools):
    organization = identity.active_organization if identity.role == "SHVYA_SUPERADMIN" else identity.organization
    live = set(effective_capabilities(role=identity.role, organization=organization))
    granted = set(identity.granted_capabilities)
    effective = live & granted
    rows = []
    for item in tools:
        name = item.get("name")
        capability = item.get("capability")
        allowed = capability is None or capability in effective or name == "get_operations_context"
        rows.append({
            "name": name,
            "available": allowed,
            "capability": capability,
            "approval_required": bool(capability and organization and approval_required(role=identity.role, organization=organization, capability=capability)),
            "dependencies": ["operations.read"] + (["operations.write"] if any(scope.get("type") == "oauth2" and "operations.write" in (scope.get("scopes") or []) for scope in (item.get("securitySchemes") or [])) else []),
            "environment_limitations": [],
        })
    return {
        "role": identity.role,
        "organization": {"id": str(organization.id), "name": organization.name, "active": organization.is_active} if organization else None,
        "oauth_scopes": sorted(identity.scopes),
        "granted_capabilities": sorted(granted),
        "effective_capabilities": sorted(effective),
        "tools": rows,
        "unsupported_features": [
            {"name": "Voice Agent", "status": "excluded_from_scope", "reason": "Handled separately."},
            {"name": "Vault", "status": "excluded_from_scope", "reason": "Handled separately."},
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
            "Live provider tests are limited to explicit readiness checks exposed by the provider integration.",
        ],
    }
