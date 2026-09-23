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
        PLATFORM_EMAIL_UPDATED = "platform_email_updated", "Platform email updated"
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
        ORGANIZATION_DELETED = "organization_deleted", "Organization deleted"
        ORGANIZATION_TAG_UPDATED = "organization_tag_updated", "Organization tag updated"
        ORGANIZATION_TAG_DELETED = "organization_tag_deleted", "Organization tag deleted"
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

class PlatformEmailConfiguration(models.Model):
    """Singleton sender for platform-owned verification emails, never tenant mail."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    email_address = models.EmailField()
    sender_name = models.CharField(max_length=120, default="SHVYA AI")
    smtp_host = models.CharField(max_length=255, default="smtp.gmail.com")
    smtp_port = models.PositiveIntegerField(default=587)
    smtp_security = models.CharField(max_length=16, choices=[("starttls", "STARTTLS"), ("ssl", "SSL/TLS")], default="starttls")
    smtp_username = models.CharField(max_length=254)
    encrypted_password = models.TextField(blank=True)
    is_enabled = models.BooleanField(default=False)
    last_tested_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=100, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name="singleton_platform_email")]

    def __str__(self):
        return "Platform email configuration"

    def as_smtp_configuration(self):
        from apps.integrations.models import EmailConfiguration
        return EmailConfiguration(**{name: getattr(self, name) for name in (
            "email_address", "sender_name", "smtp_host", "smtp_port", "smtp_security",
            "smtp_username", "encrypted_password",
        )})

    def set_password(self, password):
        configuration = self.as_smtp_configuration()
        configuration.set_password(password)
        self.encrypted_password = configuration.encrypted_password
