import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0011_diagnostic_audit_protect"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="OperationsConfigurationPlan",
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
                ("role", models.CharField(max_length=32)),
                ("idempotency_key", models.CharField(blank=True, max_length=128)),
                ("reason", models.CharField(max_length=500)),
                ("operations", models.JSONField(default=list)),
                ("base_etag", models.CharField(db_index=True, max_length=64)),
                ("plan_hash", models.CharField(db_index=True, max_length=64)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("ready", "Ready"),
                            ("applied", "Applied"),
                            ("rolled_back", "Rolled back"),
                            ("failed", "Failed"),
                            ("expired", "Expired"),
                        ],
                        db_index=True,
                        default="ready",
                        max_length=16,
                    ),
                ),
                ("reversible", models.BooleanField(default=False)),
                ("risk_summary", models.JSONField(blank=True, default=dict)),
                ("apply_result", models.JSONField(blank=True, default=dict)),
                ("inverse_operations", models.JSONField(blank=True, default=list)),
                ("applied_etag", models.CharField(blank=True, max_length=64)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("applied_at", models.DateTimeField(blank=True, null=True)),
                ("rolled_back_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "actor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="operations_configuration_plans",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="operations_configuration_plans",
                        to="organizations.organization",
                    ),
                ),
                (
                    "token",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="configuration_plans",
                        to="integrations.operationsoauthtoken",
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddConstraint(
            model_name="operationsconfigurationplan",
            constraint=models.UniqueConstraint(
                condition=~models.Q(idempotency_key=""),
                fields=("organization", "actor", "idempotency_key"),
                name="ops_plan_org_actor_idempotency_uniq",
            ),
        ),
        migrations.AddIndex(
            model_name="operationsconfigurationplan",
            index=models.Index(
                fields=["organization", "status", "-created_at"],
                name="ops_plan_org_status_idx",
            ),
        ),
    ]
