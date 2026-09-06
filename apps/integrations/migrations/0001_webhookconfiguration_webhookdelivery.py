import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("organizations", "0004_organizationpayment"),
    ]

    operations = [
        migrations.CreateModel(
            name="WebhookConfiguration",
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
                ("endpoint_url", models.URLField(blank=True, max_length=2048)),
                ("encrypted_secret", models.TextField(blank=True)),
                ("is_enabled", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "organization",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="webhook_configuration",
                        to="organizations.organization",
                    ),
                ),
            ],
            options={
                "verbose_name": "Webhook Configuration",
                "verbose_name_plural": "Webhook Configurations",
                "ordering": ["organization__name"],
            },
        ),
        migrations.CreateModel(
            name="WebhookDelivery",
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
                ("lead_id", models.UUIDField()),
                (
                    "event_type",
                    models.CharField(
                        choices=[("create", "Create"), ("update", "Update")],
                        max_length=10,
                    ),
                ),
                ("payload", models.JSONField(default=dict)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("queued", "Queued"),
                            ("retrying", "Retrying"),
                            ("sent", "Sent"),
                            ("failed", "Failed"),
                        ],
                        default="queued",
                        max_length=12,
                    ),
                ),
                ("attempt_count", models.PositiveSmallIntegerField(default=0)),
                (
                    "response_status",
                    models.PositiveSmallIntegerField(blank=True, null=True),
                ),
                ("response_body", models.TextField(blank=True)),
                ("error_message", models.TextField(blank=True)),
                ("delivered_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="webhook_deliveries",
                        to="organizations.organization",
                    ),
                ),
                (
                    "webhook",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="deliveries",
                        to="integrations.webhookconfiguration",
                    ),
                ),
            ],
            options={
                "verbose_name": "Webhook Delivery",
                "verbose_name_plural": "Webhook Deliveries",
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(
                        fields=["organization", "status", "created_at"],
                        name="webhook_org_status_created",
                    ),
                    models.Index(
                        fields=["lead_id", "created_at"],
                        name="webhook_lead_created",
                    ),
                ],
            },
        ),
    ]
