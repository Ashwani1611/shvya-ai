import uuid
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("ai_engagement", "0019_document_file_sharing_ready")]
    operations = [migrations.CreateModel(
        name="KnowledgeRepairRequest",
        fields=[
            ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ("fingerprint", models.CharField(max_length=64)),
            ("operation", models.CharField(max_length=24)),
            ("task_id", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
            ("status", models.CharField(choices=[("queued", "Queued"), ("dispatch_failed", "Dispatch failed"),
                ("running", "Running"), ("retrying", "Retrying"), ("succeeded", "Succeeded"),
                ("failed", "Failed"), ("skipped", "Skipped")], default="queued", max_length=20)),
            ("attempt", models.PositiveSmallIntegerField(default=0)),
            ("outcome_code", models.CharField(blank=True, max_length=64)),
            ("result_document_id", models.PositiveBigIntegerField(blank=True, null=True)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("updated_at", models.DateTimeField(auto_now=True)),
            ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="organizations.organization")),
            ("document", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="ai_engagement.document")),
            ("source", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to="ai_engagement.knowledgesource")),
        ], options={
            "indexes": [models.Index(fields=["organization", "status"], name="ai_repair_org_status_idx")],
            "constraints": [models.UniqueConstraint(fields=("organization", "document", "fingerprint"), name="ai_repair_document_revision_uniq")],
        },
    )]
