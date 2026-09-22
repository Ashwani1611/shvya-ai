import django.db.models.deletion
import uuid

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0011_diagnostic_audit_protect"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="OperationsConfigurationPlan",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("role", models.CharField(max_length=32)),
                ("status", models.CharField(choices=[("draft", "Draft"), ("applied", "Applied"), ("rolled_back", "Rolled back")], db_index=True, default="draft", max_length=16)),
                ("operations", models.JSONField(default=list)),
                ("summary", models.JSONField(blank=True, default=dict)),
                ("rollback_operations", models.JSONField(blank=True, default=list)),
                ("base_etag", models.CharField(max_length=64)),
                ("applied_etag", models.CharField(blank=True, max_length=64)),
                ("plan_digest", models.CharField(db_index=True, max_length=64)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("applied_at", models.DateTimeField(blank=True, null=True)),
                ("rolled_back_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("actor", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="operations_configuration_plans", to=settings.AUTH_USER_MODEL)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="operations_configuration_plans", to="organizations.organization")),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddIndex(
            model_name="operationsconfigurationplan",
            index=models.Index(fields=["organization", "status", "-created_at"], name="ops_cfg_plan_org_status_idx"),
        ),
        migrations.AddIndex(
            model_name="operationsconfigurationplan",
            index=models.Index(fields=["actor", "status", "-created_at"], name="ops_cfg_plan_actor_status_idx"),
        ),
    ]
