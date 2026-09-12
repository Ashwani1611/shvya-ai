import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0020_pipeline_ai_enabled"),
        ("integrations", "0002_emailconfiguration"),
    ]

    operations = [
        migrations.CreateModel(
            name="GoogleSheetIntegration",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("name", models.CharField(default="Google Sheets Leads", max_length=120)),
                ("spreadsheet_id", models.CharField(blank=True, max_length=255)),
                ("spreadsheet_url", models.URLField(blank=True, max_length=2048)),
                ("sheet_id", models.CharField(blank=True, max_length=64)),
                ("worksheet_name", models.CharField(default="Sheet1", max_length=180)),
                ("mapping", models.JSONField(blank=True, default=dict)),
                ("discovered_headers", models.JSONField(blank=True, default=list)),
                (
                    "webhook_token",
                    models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
                ),
                ("encrypted_secret", models.TextField(blank=True)),
                ("import_existing", models.BooleanField(default=True)),
                ("is_enabled", models.BooleanField(default=False)),
                ("last_registered_at", models.DateTimeField(blank=True, null=True)),
                ("last_synced_at", models.DateTimeField(blank=True, null=True)),
                ("last_error", models.TextField(blank=True)),
                ("created_count", models.PositiveBigIntegerField(default=0)),
                ("updated_count", models.PositiveBigIntegerField(default=0)),
                ("skipped_count", models.PositiveBigIntegerField(default=0)),
                ("error_count", models.PositiveBigIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="google_sheet_integrations",
                        to="organizations.organization",
                    ),
                ),
                (
                    "pipeline",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="google_sheet_integrations",
                        to="crm.pipeline",
                    ),
                ),
                (
                    "stage",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="google_sheet_integrations",
                        to="crm.stage",
                    ),
                ),
            ],
            options={
                "verbose_name": "Google Sheets Integration",
                "verbose_name_plural": "Google Sheets Integrations",
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(
                        fields=["organization", "is_enabled", "created_at"],
                        name="gsheet_org_enabled_created",
                    )
                ],
            },
        ),
    ]
