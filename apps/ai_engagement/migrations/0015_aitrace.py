import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ai_engagement", "0014_ai_credit_wallet"),
        ("crm", "0024_repair_lead_pipeline_stage_consistency"),
        ("organizations", "0004_organizationpayment"),
    ]

    operations = [
        migrations.CreateModel(
            name="AITrace",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("pipeline_id", models.UUIDField(blank=True, null=True)),
                ("stage_id", models.UUIDField(blank=True, null=True)),
                ("whatsapp_account_id", models.UUIDField(blank=True, null=True)),
                ("source_inbound_message_id", models.UUIDField(db_index=True)),
                ("outbound_message_id", models.UUIDField(blank=True, null=True)),
                ("connection_type", models.CharField(choices=[("api", "API"), ("hosted", "Hosted")], max_length=16)),
                ("status", models.CharField(choices=[("started", "Started"), ("processing", "Processing"), ("completed", "Completed"), ("blocked", "Blocked"), ("failed", "Failed"), ("stale", "Stale"), ("duplicate", "Duplicate"), ("silenced", "Silenced")], default="started", max_length=16)),
                ("reason_code", models.CharField(blank=True, db_index=True, max_length=96)),
                ("execution_path", models.CharField(blank=True, max_length=32)),
                ("model_name", models.CharField(blank=True, max_length=150)),
                ("incoming_message_preview", models.CharField(blank=True, max_length=500)),
                ("response_preview", models.CharField(blank=True, max_length=500)),
                ("total_ms", models.PositiveIntegerField(blank=True, null=True)),
                ("details", models.JSONField(blank=True, default=dict)),
                ("started_at", models.DateTimeField(auto_now_add=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("lead", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="ai_traces", to="crm.lead")),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="ai_traces", to="organizations.organization")),
            ],
            options={"ordering": ["-started_at", "-id"]},
        ),
        migrations.AddIndex(model_name="aitrace", index=models.Index(fields=["organization", "-started_at"], name="ai_trace_org_created_idx")),
        migrations.AddIndex(model_name="aitrace", index=models.Index(fields=["organization", "lead", "-started_at"], name="ai_trace_org_lead_idx")),
        migrations.AddIndex(model_name="aitrace", index=models.Index(fields=["organization", "status", "-started_at"], name="ai_trace_org_status_idx")),
        migrations.AddIndex(model_name="aitrace", index=models.Index(fields=["organization", "connection_type", "-started_at"], name="ai_trace_org_conn_idx")),
    ]
