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


class ImmutableOperationsAuditQuerySet(models.QuerySet):
    """Prevent ORM mutation/deletion of append-only Operations audit rows."""

    def update(self, **kwargs):
        raise ValidationError("Operations audit events are immutable.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError("Operations audit events are immutable.")

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        if update_conflicts:
            raise ValidationError("Operations audit events are immutable.")
        return super().bulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=update_conflicts,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )

    def delete(self):
        raise ValidationError("Operations audit events are immutable.")


class OperationsAuditManager(models.Manager.from_queryset(ImmutableOperationsAuditQuerySet)):
    """Manager that preserves create/read access but blocks audit rewrites."""

    pass


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
    granted_capabilities = models.JSONField(
        default=list,
        blank=True,
        help_text="Consent-time Operations capability snapshot.",
    )
    resource = models.URLField(max_length=2048)
    expires_at = models.DateTimeField(db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["client", "used_at", "expires_at"],
                name="ops_code_client_exp_idx",
            ),
            models.Index(
                fields=["actor", "used_at", "expires_at"],
                name="ops_code_actor_exp_idx",
            ),
        ]


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
    granted_capabilities = models.JSONField(
        default=list,
        blank=True,
        help_text="Consent-time Operations capability snapshot.",
    )
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
            ),
            models.Index(
                fields=["role", "revoked_at", "refresh_expires_at"],
                name="ops_token_role_grant_idx",
            ),
            models.Index(
                fields=["organization", "role", "revoked_at"],
                name="ops_token_org_role_idx",
            ),
            models.Index(
                fields=["client", "revoked_at", "refresh_expires_at"],
                name="ops_token_client_exp_idx",
            ),
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
            ),
            models.Index(
                fields=["token", "ended_at", "-last_seen_at"],
                name="ops_support_token_idx",
            ),
        ]


class OperationsAuditEvent(models.Model):
    """Append-only safe audit event for every Operations MCP tool call."""

    objects = OperationsAuditManager()

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
    actor_reference = models.UUIDField(null=True, editable=False)
    organization_reference = models.UUIDField(null=True, editable=False)
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
        self.actor_reference = self.actor_id
        self.organization_reference = self.organization_id
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Operations audit events are immutable.")



class ImmutableOperationsApprovalUseQuerySet(models.QuerySet):
    """Prevent a consumed approval receipt from becoming reusable."""

    def update(self, **kwargs):
        raise ValidationError("Operations approval uses are immutable.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError("Operations approval uses are immutable.")

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        if update_conflicts:
            raise ValidationError("Operations approval uses are immutable.")
        return super().bulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=update_conflicts,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )

    def delete(self):
        raise ValidationError("Operations approval uses are immutable.")


class OperationsApprovalUseManager(
    models.Manager.from_queryset(ImmutableOperationsApprovalUseQuerySet)
):
    pass


class OperationsApprovalUse(models.Model):
    """Single-use claim on one approval-required Operations dry-run audit event."""

    objects = OperationsApprovalUseManager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    approval_event = models.OneToOneField(
        OperationsAuditEvent,
        on_delete=models.PROTECT,
        related_name="approval_use",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Approval use — {self.approval_event_id}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Operations approval uses are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Operations approval uses are immutable.")


class OperationsConfigurationPlan(models.Model):
    """Durable actor/tenant-bound multi-object Operations configuration plan."""

    class Status(models.TextChoices):
        READY = "ready", "Ready"
        APPLIED = "applied", "Applied"
        ROLLED_BACK = "rolled_back", "Rolled back"
        FAILED = "failed", "Failed"
        EXPIRED = "expired", "Expired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="operations_configuration_plans",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        related_name="operations_configuration_plans",
    )
    token = models.ForeignKey(
        OperationsOAuthToken,
        on_delete=models.RESTRICT,
        related_name="configuration_plans",
    )
    role = models.CharField(max_length=32)
    idempotency_key = models.CharField(max_length=128, blank=True)
    reason = models.CharField(max_length=500)
    operations = models.JSONField(default=list)
    base_etag = models.CharField(max_length=64, db_index=True)
    plan_hash = models.CharField(max_length=64, db_index=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.READY,
        db_index=True,
    )
    reversible = models.BooleanField(default=False)
    risk_summary = models.JSONField(default=dict, blank=True)
    apply_result = models.JSONField(default=dict, blank=True)
    inverse_operations = models.JSONField(default=list, blank=True)
    applied_etag = models.CharField(max_length=64, blank=True)
    expires_at = models.DateTimeField(db_index=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    rolled_back_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "actor", "idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="ops_plan_org_actor_idempotency_uniq",
            )
        ]
        indexes = [
            models.Index(
                fields=["organization", "status", "-created_at"],
                name="ops_plan_org_status_idx",
            )
        ]

    def __str__(self):
        return f"Operations plan {self.id} — {self.organization_id} — {self.status}"


class OperationsCommitment(models.Model):
    """Tenant-scoped follow-up work captured from onboarding and audits.

    This is deliberately an operational work item, not a provider task or
    outbound message. It gives onboarding, unresolved integrations, and audit
    findings one durable, auditable place to land without granting any send or
    automation authority.
    """

    class Source(models.TextChoices):
        ONBOARDING_CALL = "onboarding_call", "Onboarding call"
        INTEGRATION = "integration", "Integration"
        AUDIT = "audit", "Audit"
        ACCEPTANCE = "acceptance", "Acceptance test"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In progress"
        BLOCKED = "blocked", "Blocked"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="operations_commitments",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_operations_commitments",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owned_operations_commitments",
    )
    source = models.CharField(max_length=32, choices=Source.choices)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.OPEN,
        db_index=True,
    )
    due_at = models.DateTimeField(null=True, blank=True, db_index=True)
    source_reference = models.CharField(max_length=160, blank=True)
    resolution = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["status", "due_at", "-created_at"]
        indexes = [
            models.Index(
                fields=["organization", "status", "due_at"],
                name="ops_commit_org_status_due_idx",
            ),
        ]


class OperationsAcceptanceRun(models.Model):
    """Immutable result envelope for a no-send acceptance suite."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="operations_acceptance_runs",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="operations_acceptance_runs",
    )
    suite = models.CharField(max_length=64)
    status = models.CharField(max_length=20)
    cases = models.JSONField(default=list, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["organization", "suite", "-created_at"],
                name="ops_accept_org_suite_idx",
            ),
        ]
