"""Organization-level feature flags stored in Organization.settings."""

HOSTED_ACCOUNT_SETTING_KEY = "hosted_account_enabled"


def is_hosted_account_enabled(organization):
    """Return whether Hosted Account access is enabled for an organization.

    Missing or malformed settings are intentionally treated as disabled so the
    feature is off by default for both existing and newly-created organizations.
    """
    if getattr(organization, "package", None) == "enterprise":
        return True
    settings = getattr(organization, "settings", None)
    if not isinstance(settings, dict):
        return False
    return settings.get(HOSTED_ACCOUNT_SETTING_KEY) is True


def set_hosted_account_enabled(organization, enabled):
    """Persist the Hosted Account feature flag for an organization."""
    settings = getattr(organization, "settings", None)
    settings = dict(settings) if isinstance(settings, dict) else {}
    settings[HOSTED_ACCOUNT_SETTING_KEY] = bool(enabled)
    organization.settings = settings
    organization.save(update_fields=["settings", "updated_at"])
    return organization


MODULE_LABELS = {
    "sales_desk": "Sales Desk",
    "calendar": "SHVYA Calendar",
    "calls": "CALL INTELLIGENCE",
    "sales": "SHVYA Sales",
    "cadence": "Cadence",
    "playbooks": "Playbooks",
    "workflows": "Workflows",
    "whatsapp": "WhatsApp",
    "instagram": "Instagram",
}
PLAN_RESTRICTIONS = {
    "free": frozenset(MODULE_LABELS),
    "diy": frozenset({"calendar", "calls", "sales", "instagram"}),
    "dfy": frozenset({"calls", "sales"}),
    "enterprise": frozenset(),
}
MODULE_PATHS = {
    "sales_desk": ("/dashboard/sales-desk/", "/dashboard/copilot/", "/api/v1/copilot/"),
    "calendar": ("/dashboard/shvya-calendar/",),
    "calls": ("/dashboard/call-intelligence/", "/api/v1/call-intelligence/"),
    "sales": ("/dashboard/sales/",),
    "cadence": ("/dashboard/cadence/", "/dashboard/auto-follow-ups/"),
    "playbooks": ("/dashboard/playbooks/", "/dashboard/knowledge-base/"),
    "workflows": ("/dashboard/workflows/", "/dashboard/smart-triggers/"),
    "whatsapp": ("/dashboard/whatsapp/",),
    "instagram": ("/dashboard/instagram/",),
}


def module_for_path(path):
    return next(
        (
            key
            for key, prefixes in MODULE_PATHS.items()
            if any(
                path.startswith(prefix) or path == prefix.rstrip("/")
                for prefix in prefixes
            )
        ),
        None,
    )


def module_enabled(organization, module):
    if organization is None:
        return False
    package = organization.package
    restricted = PLAN_RESTRICTIONS.get(package, PLAN_RESTRICTIONS["free"])
    if module not in restricted:
        return True
    # Free modules stay locked until a package upgrade. Overrides are only
    # grants for the current paid package, so old grants cannot leak on downgrade.
    settings = organization.settings if isinstance(organization.settings, dict) else {}
    grants = settings.get("package_module_grants", {})
    return (
        package in {"diy", "dfy"}
        and isinstance(grants, dict)
        and isinstance(grants.get(package), list)
        and module in grants[package]
    )


def module_controls(organization):
    return [
        {
            "key": key,
            "label": MODULE_LABELS[key],
            "enabled": module_enabled(organization, key),
        }
        for key in MODULE_LABELS
        if key in PLAN_RESTRICTIONS.get(organization.package, ())
        and organization.package in {"diy", "dfy"}
    ]
