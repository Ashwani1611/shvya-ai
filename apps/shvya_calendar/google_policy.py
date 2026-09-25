"""Tenant-owned Google connections are primary; SHVYA hosting is opt-in.

Only non-secret preferences live in Organization.settings. Existing OAuth
credentials remain in encrypted host connections or the protected backend env.
"""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.organizations.models import Organization

POLICY_KEY = "calendar_google"
ORGANIZATION_ONLY = "organization_only"
ORGANIZATION_WITH_FALLBACK = "organization_with_fallback"
GOOGLE_MODE_CHOICES = (
    (ORGANIZATION_ONLY, "Use my organisation's Google account (recommended)"),
    (
        ORGANIZATION_WITH_FALLBACK,
        "Use my organisation's account, with SHVYA-managed fallback",
    ),
)


def settings_allow_platform_fallback(settings):
    """Require explicit JSON boolean consent; malformed values fail closed."""
    policy = settings.get(POLICY_KEY) if isinstance(settings, dict) else None
    return isinstance(policy, dict) and policy.get("allow_platform_fallback") is True


def organization_allows_platform_fallback(organization_id):
    """Read current tenant consent instead of a worker's cached related object."""
    settings = Organization.objects.filter(
        pk=organization_id, is_active=True,
    ).values_list("settings", flat=True).first()
    return settings_allow_platform_fallback(settings)


def save_google_mode(*, actor, mode):
    """Update only the authenticated actor's tenant; preserve unrelated settings."""
    if mode not in dict(GOOGLE_MODE_CHOICES):
        raise ValidationError("Choose a supported Google hosting preference.")
    if not actor.pk or not actor.organization_id:
        raise PermissionDenied("An organisation administrator is required.")
    with transaction.atomic():
        # Revalidate current role and tenant rather than trusting posted IDs or
        # a stale session. Lock the actor while authorising this short write.
        current_actor = User.objects.select_for_update().filter(
            pk=actor.pk, organization_id=actor.organization_id,
            role=User.Role.ADMIN, is_active=True,
        ).first()
        if current_actor is None:
            raise PermissionDenied(
                "Only an active organisation administrator can change Google hosting."
            )
        organization = Organization.objects.select_for_update().filter(
            pk=current_actor.organization_id, is_active=True,
        ).first()
        if organization is None:
            raise PermissionDenied("The organisation is unavailable.")
        if not isinstance(organization.settings, dict):
            raise ValidationError(
                "Organisation settings must be repaired before changing Google hosting."
            )
        settings = dict(organization.settings)
        existing = settings.get(POLICY_KEY, {})
        if not isinstance(existing, dict):
            raise ValidationError(
                "Google hosting settings must be repaired before changing this preference."
            )
        policy = dict(existing)
        policy.update(
            allow_platform_fallback=mode == ORGANIZATION_WITH_FALLBACK,
            updated_by=str(current_actor.pk),
            updated_at=timezone.now().isoformat(),
        )
        settings[POLICY_KEY] = policy
        organization.settings = settings
        organization.save(update_fields=["settings", "updated_at"])
    # No immediate task dispatch, invitations or event migration. The existing
    # recovery task handles future unconnected bookings for opted-in tenants.
    return organization
