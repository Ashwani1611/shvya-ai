"""Persistence for the actor-bound SHVYA Operations MCP.

The existing diagnostic connector intentionally remains read-only.  These
models back a separate operations boundary where OAuth tokens are bound to a
real SHVYA human, organization policy is Superadmin-controlled, support context
is explicit, and every tool call is auditable without storing raw prompts,
conversations, credentials, or provider payloads.
"""

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class OperationsPolicy(models.Model):
    """Superadmin-owned external-AI policy for one customer organization."""

    organization = models.OneToOneField(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="operations_mcp_policy",
    )
    organization_admin_enabled = models.BooleanField(default=False)
    allowed_capabilities = models.JSONField(default=list, blank=True)
    approval_required_capabilities = models.JSONField(default=list, blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["organization__name"]

    def __str__(self):
        return f"Operations MCP policy — {self.organization.name}"


class OperationsOAuthClient(models.Model):
    """Dynamically registered public OAuth client for ChatGPT/Claude."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client_id = models.CharField(max_length=255, unique=True, db_index=True)
    client_name = models.CharField(max_length=200, blank=True)
    application_type = models.CharField(max_length=32, default="web")
    redirect_uris = models.JSONField(default=list)
    grant_types = models.JSONField(default=list)
    response_types = models.JSONField(default=list)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class OperationsOAuthAuthorizationCode(models.Model):
    """One-time PKCE code bound to the authenticated SHVYA human."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(
        OperationsOAuthClient,
        on_delete=models.CASCADE,
        related_name="authorization_codes",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="operations_oauth_authorization_codes",
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="operations_oauth_authorization_codes",
    )
    role = models.CharField(max_length=32)
    code_hash = models.CharField(max_length=64, unique=True, db_index=True)
    redirect_uri = models.URLField(max_length=2048)
    code_challenge = models.CharField(max_length=128)
    scope = models.CharField(max_length=512)
    resource = models.URLField(max_length=2048)
    expires_at = models.DateTimeField(db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class OperationsOAuthToken(models.Model):
    """Hashed Operations MCP bearer/refresh tokens with explicit actor scope."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(
        OperationsOAuthClient,
        on_delete=models.CASCADE,
        related_name="tokens",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="operations_oauth_tokens",
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="operations_oauth_tokens",
        help_text="Fixed organization for organization-admin tokens.",
    )
    active_organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="Explicit current customer support context for Superadmin tokens.",
    )
    role = models.CharField(max_length=32)
    access_token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    refresh_token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    scope = models.CharField(max_length=512)
    resource = models.URLField(max_length=2048)
    expires_at = models.DateTimeField(db_index=True)
    refresh_expires_at = models.DateTimeField(db_index=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["actor", "revoked_at", "expires_at"],
                name="ops_token_actor_access_idx",
            )
        ]


class OperationsSupportSession(models.Model):
    """Visible record of a Superadmin actively operating inside one tenant."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    token = models.ForeignKey(
        OperationsOAuthToken,
        on_delete=models.CASCADE,
        related_name="support_sessions",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="operations_support_sessions",
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="operations_support_sessions",
    )
    reason = models.CharField(max_length=500, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [
            models.Index(
                fields=["organization", "ended_at", "-last_seen_at"],
                name="ops_support_org_active_idx",
            )
        ]


class OperationsAuditEvent(models.Model):
    """Append-only safe audit event for every Operations MCP tool call."""

    class Outcome(models.TextChoices):
        SUCCESS = "success", "Success"
        ERROR = "error", "Error"
        DENIED = "denied", "Denied"
        APPROVAL_REQUIRED = "approval_required", "Approval required"
        DRY_RUN = "dry_run", "Dry run"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="operations_mcp_audit_events",
    )
    role = models.CharField(max_length=32)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="operations_mcp_audit_events",
    )
    support_session = models.ForeignKey(
        OperationsSupportSession,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_events",
    )
    tool_name = models.CharField(max_length=100)
    capability = models.CharField(max_length=100, blank=True)
    target_type = models.CharField(max_length=80, blank=True)
    target_id = models.CharField(max_length=100, blank=True)
    reason = models.CharField(max_length=500, blank=True)
    outcome = models.CharField(max_length=24, choices=Outcome.choices)
    request_fingerprint = models.CharField(max_length=64)
    change_summary = models.JSONField(default=dict, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["organization", "-created_at"],
                name="ops_audit_org_created_idx",
            ),
            models.Index(
                fields=["actor", "-created_at"],
                name="ops_audit_actor_created_idx",
            ),
            models.Index(
                fields=["tool_name", "-created_at"],
                name="ops_audit_tool_created_idx",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Operations audit events are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Operations audit events are immutable.")
