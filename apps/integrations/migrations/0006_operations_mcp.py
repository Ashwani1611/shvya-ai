import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
import uuid


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0005_diagnostic_mcp_oauth"),
        ("organizations", "0005_apikey_can_read_diagnostics"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="OperationsOAuthClient",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("client_id", models.CharField(db_index=True, max_length=255, unique=True)),
                ("client_name", models.CharField(blank=True, max_length=200)),
                ("application_type", models.CharField(default="web", max_length=32)),
                ("redirect_uris", models.JSONField(default=list)),
                ("grant_types", models.JSONField(default=list)),
                ("response_types", models.JSONField(default=list)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="OperationsPolicy",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("organization_admin_enabled", models.BooleanField(default=False)),
                ("allowed_capabilities", models.JSONField(blank=True, default=list)),
                ("approval_required_capabilities", models.JSONField(blank=True, default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("organization", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="operations_mcp_policy", to="organizations.organization")),
                ("updated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["organization__name"]},
        ),
        migrations.CreateModel(
            name="OperationsOAuthAuthorizationCode",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("role", models.CharField(max_length=32)),
                ("code_hash", models.CharField(db_index=True, max_length=64, unique=True)),
                ("redirect_uri", models.URLField(max_length=2048)),
                ("code_challenge", models.CharField(max_length=128)),
                ("scope", models.CharField(max_length=512)),
                ("resource", models.URLField(max_length=2048)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("actor", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="operations_oauth_authorization_codes", to=settings.AUTH_USER_MODEL)),
                ("client", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="authorization_codes", to="integrations.operationsoauthclient")),
                ("organization", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="operations_oauth_authorization_codes", to="organizations.organization")),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="OperationsOAuthToken",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("role", models.CharField(max_length=32)),
                ("access_token_hash", models.CharField(db_index=True, max_length=64, unique=True)),
                ("refresh_token_hash", models.CharField(db_index=True, max_length=64, unique=True)),
                ("scope", models.CharField(max_length=512)),
                ("resource", models.URLField(max_length=2048)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("refresh_expires_at", models.DateTimeField(db_index=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("active_organization", models.ForeignKey(blank=True, help_text="Explicit current customer support context for Superadmin tokens.", null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="organizations.organization")),
                ("actor", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="operations_oauth_tokens", to=settings.AUTH_USER_MODEL)),
                ("client", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="tokens", to="integrations.operationsoauthclient")),
                ("organization", models.ForeignKey(blank=True, help_text="Fixed organization for organization-admin tokens.", null=True, on_delete=django.db.models.deletion.CASCADE, related_name="operations_oauth_tokens", to="organizations.organization")),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [models.Index(fields=["actor", "revoked_at", "expires_at"], name="ops_token_actor_access_idx")],
            },
        ),
        migrations.CreateModel(
            name="OperationsSupportSession",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("reason", models.CharField(blank=True, max_length=500)),
                ("started_at", models.DateTimeField(auto_now_add=True)),
                ("last_seen_at", models.DateTimeField(auto_now_add=True)),
                ("ended_at", models.DateTimeField(blank=True, null=True)),
                ("actor", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="operations_support_sessions", to=settings.AUTH_USER_MODEL)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="operations_support_sessions", to="organizations.organization")),
                ("token", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="support_sessions", to="integrations.operationsoauthtoken")),
            ],
            options={
                "ordering": ["-started_at"],
                "indexes": [models.Index(fields=["organization", "ended_at", "-last_seen_at"], name="ops_support_org_active_idx")],
            },
        ),
        migrations.CreateModel(
            name="OperationsAuditEvent",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("role", models.CharField(max_length=32)),
                ("tool_name", models.CharField(max_length=100)),
                ("capability", models.CharField(blank=True, max_length=100)),
                ("target_type", models.CharField(blank=True, max_length=80)),
                ("target_id", models.CharField(blank=True, max_length=100)),
                ("reason", models.CharField(blank=True, max_length=500)),
                ("outcome", models.CharField(choices=[("success", "Success"), ("error", "Error"), ("denied", "Denied"), ("approval_required", "Approval required"), ("dry_run", "Dry run")], max_length=24)),
                ("request_fingerprint", models.CharField(max_length=64)),
                ("change_summary", models.JSONField(blank=True, default=dict)),
                ("duration_ms", models.PositiveIntegerField(default=0)),
                ("error_code", models.CharField(blank=True, max_length=100)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("actor", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="operations_mcp_audit_events", to=settings.AUTH_USER_MODEL)),
                ("organization", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="operations_mcp_audit_events", to="organizations.organization")),
                ("support_session", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="audit_events", to="integrations.operationssupportsession")),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(fields=["organization", "-created_at"], name="ops_audit_org_created_idx"),
                    models.Index(fields=["actor", "-created_at"], name="ops_audit_actor_created_idx"),
                    models.Index(fields=["tool_name", "-created_at"], name="ops_audit_tool_created_idx"),
                ],
            },
        ),
    ]
