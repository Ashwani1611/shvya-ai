import uuid

from django.conf import settings
from django.db import models


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")

    if forwarded:
        return forwarded.split(",")[0].strip()

    return request.META.get("REMOTE_ADDR")


class AuditLog(models.Model):
    """
    Immutable log of sensitive Superadmin actions.

    Records WHO (actor) did WHAT (action) to WHOM/WHAT (target),
    from WHERE (ip_address), and WHEN (created_at).

    This is intentionally append-only: there is no update path,
    and nothing in this app should ever call .save() on an
    existing row or .delete() on one.
    """

    class Action(models.TextChoices):
        LOGIN_LINK_GENERATED = (
            "login_link_generated",
            "Login link generated",
        )
        PASSWORD_RESET = (
            "password_reset",
            "Password reset",
        )
        USER_ACTIVATED = (
            "user_activated",
            "User activated",
        )
        USER_DEACTIVATED = (
            "user_deactivated",
            "User deactivated",
        )
        USER_CREATED = (
            "user_created",
            "User created",
        )
        ORGANIZATION_CREATED = (
            "organization_created",
            "Organization created",
        )
        ORGANIZATION_UPDATED = (
            "organization_updated",
            "Organization updated",
        )

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="audit_actions",
    )

    action = models.CharField(
        max_length=32,
        choices=Action.choices,
    )

    # Generic target reference kept as plain fields (not a real
    # FK / GenericForeignKey) so this model has zero dependency
    # on which apps exist -- it can log against Users, Orgs,
    # Pipelines, whatever, without an import cycle.
    target_type = models.CharField(max_length=64, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    target_repr = models.CharField(max_length=255, blank=True)

    ip_address = models.GenericIPAddressField(null=True, blank=True)

    metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["action", "created_at"]),
            models.Index(fields=["actor", "created_at"]),
        ]

    def __str__(self):
        return f"{self.actor_id} -> {self.action} -> {self.target_repr}"

    @classmethod
    def record(
        cls,
        *,
        actor,
        action,
        target=None,
        request=None,
        **metadata,
    ):
        """
        Convenience constructor.

        Usage:

            AuditLog.record(
                actor=request.user,
                action=AuditLog.Action.PASSWORD_RESET,
                target=selected_user,
                request=request,
                organization_id=str(organization.id),
            )
        """

        ip_address = _client_ip(request) if request is not None else None

        return cls.objects.create(
            actor=actor,
            action=action,
            target_type=target.__class__.__name__ if target else "",
            target_id=str(getattr(target, "pk", "")) if target else "",
            target_repr=str(target) if target else "",
            ip_address=ip_address,
            metadata=metadata,
        )