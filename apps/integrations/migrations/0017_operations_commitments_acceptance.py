from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0016_operations_oauth_lookup_indexes"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="OperationsCommitment",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("source", models.CharField(choices=[("onboarding_call", "Onboarding call"), ("integration", "Integration"), ("audit", "Audit"), ("acceptance", "Acceptance test")], max_length=32)),
                ("title", models.CharField(max_length=200)),
                ("description", models.TextField(blank=True)),
                ("status", models.CharField(choices=[("open", "Open"), ("in_progress", "In progress"), ("blocked", "Blocked"), ("completed", "Completed"), ("cancelled", "Cancelled")], db_index=True, default="open", max_length=20)),
                ("due_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("source_reference", models.CharField(blank=True, max_length=160)),
                ("resolution", models.TextField(blank=True)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("created_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_operations_commitments", to=settings.AUTH_USER_MODEL)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="operations_commitments", to="organizations.organization")),
                ("owner", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="owned_operations_commitments", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["status", "due_at", "-created_at"],
            },
        ),
        migrations.CreateModel(
            name="OperationsAcceptanceRun",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("suite", models.CharField(max_length=64)),
                ("status", models.CharField(max_length=20)),
                ("cases", models.JSONField(blank=True, default=list)),
                ("summary", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("actor", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="operations_acceptance_runs", to=settings.AUTH_USER_MODEL)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="operations_acceptance_runs", to="organizations.organization")),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddIndex(
            model_name="operationscommitment",
            index=models.Index(fields=["organization", "status", "due_at"], name="ops_commit_org_status_due_idx"),
        ),
        migrations.AddIndex(
            model_name="operationsacceptancerun",
            index=models.Index(fields=["organization", "suite", "-created_at"], name="ops_accept_org_suite_idx"),
        ),
    ]
