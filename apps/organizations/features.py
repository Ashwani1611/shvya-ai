"""Organization-level feature flags stored in Organization.settings."""

HOSTED_ACCOUNT_SETTING_KEY = "hosted_account_enabled"


def is_hosted_account_enabled(organization):
    """Return whether Hosted Account access is enabled for an organization.

    Missing or malformed settings are intentionally treated as disabled so the
    feature is off by default for both existing and newly-created organizations.
    """
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
