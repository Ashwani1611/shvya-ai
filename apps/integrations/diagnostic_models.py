"""OAuth and audit models for SHVYA's read-only diagnostic MCP connector."""

import uuid

from django.db import models


class DiagnosticOAuthClient(models.Model):
    """Dynamically registered public OAuth client used by ChatGPT/OpenAI."""

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

    def __str__(self):
        return self.client_name or self.client_id


class DiagnosticOAuthAuthorizationCode(models.Model):
    """Short-lived, one-time OAuth authorization code bound to PKCE."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(
        DiagnosticOAuthClient,
        on_delete=models.CASCADE,
        related_name="authorization_codes",
    )
    api_key = models.ForeignKey(
        "organizations.APIKey",
        on_delete=models.CASCADE,
        related_name="diagnostic_authorization_codes",
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="diagnostic_authorization_codes",
    )
    code_hash = models.CharField(max_length=64, unique=True, db_index=True)
    redirect_uri = models.URLField(max_length=2048)
    code_challenge = models.CharField(max_length=128)
    scope = models.CharField(max_length=255)
    resource = models.URLField(max_length=2048)
    expires_at = models.DateTimeField(db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class DiagnosticOAuthToken(models.Model):
    """Hashed OAuth access and refresh tokens for diagnostic read access."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(
        DiagnosticOAuthClient,
        on_delete=models.CASCADE,
        related_name="tokens",
    )
    api_key = models.ForeignKey(
        "organizations.APIKey",
        on_delete=models.CASCADE,
        related_name="diagnostic_oauth_tokens",
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="diagnostic_oauth_tokens",
    )
    access_token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    refresh_token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    scope = models.CharField(max_length=255)
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
                fields=["api_key", "revoked_at", "expires_at"],
                name="diag_token_key_access_idx",
            ),
        ]


class DiagnosticAccessLog(models.Model):
    """Append-only metadata about diagnostic tool usage.

    Deliberately stores only a hash of tool arguments. Conversation text,
    lead attributes, API keys, access tokens, and provider errors are never
    persisted in this audit table.
    """

    class Outcome(models.TextChoices):
        SUCCESS = "success", "Success"
        ERROR = "error", "Error"
        DENIED = "denied", "Denied"

    class AuthType(models.TextChoices):
        OAUTH = "oauth", "OAuth"
        API_KEY = "api_key", "API Key"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="diagnostic_access_logs",
    )
    api_key = models.ForeignKey(
        "organizations.APIKey",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="diagnostic_access_logs",
    )
    oauth_client_id = models.CharField(max_length=255, blank=True)
    tool_name = models.CharField(max_length=100)
    outcome = models.CharField(
        max_length=12,
        choices=Outcome.choices,
        default=Outcome.SUCCESS,
    )
    auth_type = models.CharField(
        max_length=12,
        choices=AuthType.choices,
        default=AuthType.OAUTH,
    )
    request_fingerprint = models.CharField(max_length=64)
    duration_ms = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["organization", "created_at"],
                name="diag_log_org_created_idx",
            ),
            models.Index(
                fields=["tool_name", "created_at"],
                name="diag_log_tool_created_idx",
            ),
        ]

    def __str__(self):
        return f"{self.organization_id} · {self.tool_name} · {self.outcome}"
