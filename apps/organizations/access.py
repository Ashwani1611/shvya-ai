"""Central authorization helpers for organization-scoped access."""


def organization_is_active(organization):
    """Return True only for a present, active organization."""

    return bool(
        organization is not None
        and getattr(organization, "is_active", False)
    )


def crm_user_is_authorized(user):
    """Return True only for an active CRM user in an active organization."""

    if user is None:
        return False

    if not getattr(user, "is_active", False):
        return False

    if getattr(user, "is_superuser", False):
        return False

    return organization_is_active(
        getattr(user, "organization", None)
    )
