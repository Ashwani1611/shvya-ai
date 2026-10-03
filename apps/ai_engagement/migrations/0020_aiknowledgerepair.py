import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("ai_engagement", "0019_document_file_sharing_ready")]
    operations = [migrations.CreateModel(
        name="AIKnowledgeRepair",
        fields=[
            ("id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
            ("action", models.CharField(max_length=32)),
            ("state", models.CharField(max_length=24, default="queued")),
            ("fingerprint", models.CharField(max_length=64)),
            ("document_fingerprint", models.CharField(max_length=64)),
            ("source_version", models.PositiveIntegerField()),
            ("result_document_id", models.PositiveBigIntegerField(null=True, blank=True)),
            ("last_attempt", models.IntegerField(default=-1)),
            ("outcome_code", models.CharField(max_length=64, blank=True)),
            ("usage_reservation_ids", models.JSONField(default=list, blank=True)),
            ("usage_truncated", models.BooleanField(default=False)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("updated_at", models.DateTimeField(auto_now=True)),
            ("organization", models.ForeignKey(to="organizations.organization", on_delete=django.db.models.deletion.CASCADE,
                                               related_name="ai_knowledge_repairs")),
            ("document", models.ForeignKey(to="ai_engagement.document", on_delete=django.db.models.deletion.CASCADE,
                                           related_name="repair_requests")),
            ("source", models.ForeignKey(to="ai_engagement.knowledgesource", on_delete=django.db.models.deletion.SET_NULL,
                                         null=True, blank=True, related_name="repair_requests")),
        ],
        options={"constraints": [models.UniqueConstraint(fields=("organization", "document"),
                    condition=models.Q(state__in=["queued", "running", "retrying", "dispatch_failed", "uncertain"]),
                    name="uniq_active_ai_source_repair")],
                 "indexes": [models.Index(fields=["organization", "state", "created_at"],
                                           name="ai_source_repair_state_idx")]},
    )]
