# Generated for SHVYA JustDial lead-push integration.

import django.db.models.deletion
import django.utils.timezone
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("crm", "0033_alter_lead_lead_source_justdial"),
        ("integrations", "0018_metaconversionsconfiguration_metaconversionmapping_and_more"),
        ("organizations", "0007_organizationdeletioncleanup"),
    ]

    operations = [
        migrations.CreateModel(
            name="JustDialIntegration",
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
                (
                    "webhook_token",
                    models.UUIDField(
                        blank=True,
                        editable=False,
                        null=True,
                        unique=True,
                    ),
                ),
                ("is_enabled", models.BooleanField(default=False)),
                ("requested_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("provisioned_at", models.DateTimeField(blank=True, null=True)),
                ("last_received_at", models.DateTimeField(blank=True, null=True)),
                ("last_error", models.CharField(blank=True, max_length=500)),
                ("received_count", models.PositiveBigIntegerField(default=0)),
                ("created_count", models.PositiveBigIntegerField(default=0)),
                ("updated_count", models.PositiveBigIntegerField(default=0)),
                ("ignored_count", models.PositiveBigIntegerField(default=0)),
                ("error_count", models.PositiveBigIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "organization",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="justdial_integration",
                        to="organizations.organization",
                    ),
                ),
                (
                    "pipeline",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.RESTRICT,
                        related_name="justdial_integrations",
                        to="crm.pipeline",
                    ),
                ),
                (
                    "stage",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.RESTRICT,
                        related_name="justdial_integrations",
                        to="crm.stage",
                    ),
                ),
            ],
            options={
                "verbose_name": "JustDial Integration",
                "verbose_name_plural": "JustDial Integrations",
                "ordering": ["organization__name"],
            },
        ),
        migrations.CreateModel(
            name="JustDialLeadEvent",
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
                ("external_lead_id", models.CharField(blank=True, max_length=160)),
                ("method", models.CharField(default="GET", max_length=8)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("created", "Created"),
                            ("updated", "Updated"),
                            ("ignored", "Ignored"),
                            ("failed", "Failed"),
                        ],
                        max_length=12,
                    ),
                ),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("error_message", models.CharField(blank=True, max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "integration",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="events",
                        to="integrations.justdialintegration",
                    ),
                ),
                (
                    "lead",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="justdial_events",
                        to="crm.lead",
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="justdial_events",
                        to="organizations.organization",
                    ),
                ),
            ],
            options={
                "verbose_name": "JustDial Lead Event",
                "verbose_name_plural": "JustDial Lead Events",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="justdialleadevent",
            index=models.Index(
                fields=["organization", "status", "created_at"],
                name="justdial_org_status_created",
            ),
        ),
        migrations.AddIndex(
            model_name="justdialleadevent",
            index=models.Index(
                fields=["integration", "external_lead_id"],
                name="justdial_integration_leadid",
            ),
        ),
    ]
