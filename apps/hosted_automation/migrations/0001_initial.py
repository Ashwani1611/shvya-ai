# Generated manually for the hosted WhatsApp automation feature.

import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("channels", "0010_whatsapptemplatemetadata_carousel_config"),
        ("followups", "0002_followupstep_recurring_weekdays"),
    ]

    operations = [
        migrations.CreateModel(
            name="HostedAccountHealth",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("enabled", models.BooleanField(default=True)),
                ("total_messages_sent", models.PositiveBigIntegerField(default=0)),
                ("window_messages_sent", models.PositiveIntegerField(default=0)),
                ("window_started_at", models.DateTimeField(blank=True, null=True)),
                ("paused_until", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("last_followup_sent_at", models.DateTimeField(blank=True, null=True)),
                ("last_followup_content_hash", models.CharField(blank=True, max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("account", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="hosted_health", to="channels.whatsappaccount")),
            ],
        ),
        migrations.CreateModel(
            name="HostedFollowupStepConfig",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("body", models.TextField()),
                ("attachment", models.FileField(blank=True, upload_to="followups/hosted/%Y/%m/%d/")),
                ("attachment_original_name", models.CharField(blank=True, max_length=255)),
                ("attachment_mime_type", models.CharField(blank=True, max_length=120)),
                ("attachment_size", models.PositiveBigIntegerField(default=0)),
                ("authored_content_hash", models.CharField(db_index=True, max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("step", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="hosted_config", to="followups.followupstep")),
            ],
        ),
        migrations.CreateModel(
            name="HostedAutomationJob",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("kind", models.CharField(choices=[("ai_engagement", "AI Engagement")], default="ai_engagement", max_length=24)),
                ("status", models.CharField(choices=[("queued", "Queued"), ("processing", "Processing"), ("completed", "Completed"), ("skipped", "Skipped"), ("failed", "Failed")], db_index=True, default="queued", max_length=12)),
                ("available_at", models.DateTimeField(db_index=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("result", models.JSONField(blank=True, default=dict)),
                ("error", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("account", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="hosted_automation_jobs", to="channels.whatsappaccount")),
                ("lead", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="hosted_automation_jobs", to="crm.lead")),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="hosted_automation_jobs", to="organizations.organization")),
                ("source_message", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="hosted_automation_job", to="channels.whatsappmessage")),
            ],
            options={"ordering": ["available_at", "created_at"]},
        ),
        migrations.AddIndex(
            model_name="hostedautomationjob",
            index=models.Index(fields=["account", "status", "available_at"], name="hosted_job_acc_due_idx"),
        ),
        migrations.AddIndex(
            model_name="hostedautomationjob",
            index=models.Index(fields=["organization", "status", "available_at"], name="hosted_job_org_due_idx"),
        ),
    ]
