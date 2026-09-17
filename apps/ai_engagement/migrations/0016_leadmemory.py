import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ai_engagement", "0015_aitrace"),
        ("crm", "0024_repair_lead_pipeline_stage_consistency"),
        ("organizations", "0004_organizationpayment"),
    ]

    operations = [
        migrations.CreateModel(
            name="LeadMemory",
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
                ("structured_facts", models.JSONField(blank=True, default=dict)),
                ("long_term_events", models.JSONField(blank=True, default=list)),
                ("source_last_message_id", models.UUIDField(blank=True, null=True)),
                ("source_last_message_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "lead",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ai_memories",
                        to="crm.lead",
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="lead_memories",
                        to="organizations.organization",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="leadmemory",
            constraint=models.UniqueConstraint(
                fields=("organization", "lead"),
                name="uniq_ai_lead_memory_org_lead",
            ),
        ),
        migrations.AddIndex(
            model_name="leadmemory",
            index=models.Index(
                fields=["organization", "lead"],
                name="ai_memory_org_lead_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="leadmemory",
            index=models.Index(
                fields=["organization", "-updated_at"],
                name="ai_memory_org_updated_idx",
            ),
        ),
    ]
