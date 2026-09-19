import django.db.models.deletion
import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0004_metaleadpage_metaleadform"),
        ("organizations", "0005_apikey_can_read_diagnostics"),
    ]

    operations = [
        migrations.CreateModel(
            name="DiagnosticOAuthClient",
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
            name="DiagnosticOAuthAuthorizationCode",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("code_hash", models.CharField(db_index=True, max_length=64, unique=True)),
                ("redirect_uri", models.URLField(max_length=2048)),
                ("code_challenge", models.CharField(max_length=128)),
                ("scope", models.CharField(max_length=255)),
                ("resource", models.URLField(max_length=2048)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("api_key", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="diagnostic_authorization_codes", to="organizations.apikey")),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="diagnostic_authorization_codes", to="organizations.organization")),
                ("client", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="authorization_codes", to="integrations.diagnosticoauthclient")),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="DiagnosticOAuthToken",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("access_token_hash", models.CharField(db_index=True, max_length=64, unique=True)),
                ("refresh_token_hash", models.CharField(db_index=True, max_length=64, unique=True)),
                ("scope", models.CharField(max_length=255)),
                ("resource", models.URLField(max_length=2048)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("refresh_expires_at", models.DateTimeField(db_index=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("api_key", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="diagnostic_oauth_tokens", to="organizations.apikey")),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="diagnostic_oauth_tokens", to="organizations.organization")),
                ("client", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="tokens", to="integrations.diagnosticoauthclient")),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="DiagnosticAccessLog",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("oauth_client_id", models.CharField(blank=True, max_length=255)),
                ("tool_name", models.CharField(max_length=100)),
                ("outcome", models.CharField(choices=[("success", "Success"), ("error", "Error"), ("denied", "Denied")], default="success", max_length=12)),
                ("auth_type", models.CharField(choices=[("oauth", "OAuth"), ("api_key", "API Key")], default="oauth", max_length=12)),
                ("request_fingerprint", models.CharField(max_length=64)),
                ("duration_ms", models.PositiveIntegerField(default=0)),
                ("error_code", models.CharField(blank=True, max_length=100)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("api_key", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="diagnostic_access_logs", to="organizations.apikey")),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="diagnostic_access_logs", to="organizations.organization")),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddIndex(
            model_name="diagnosticoauthtoken",
            index=models.Index(fields=["api_key", "revoked_at", "expires_at"], name="diag_token_key_access_idx"),
        ),
        migrations.AddIndex(
            model_name="diagnosticaccesslog",
            index=models.Index(fields=["organization", "created_at"], name="diag_log_org_created_idx"),
        ),
        migrations.AddIndex(
            model_name="diagnosticaccesslog",
            index=models.Index(fields=["tool_name", "created_at"], name="diag_log_tool_created_idx"),
        ),
    ]
