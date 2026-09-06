# Generated for SHVYA Smart Triggers.

import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("crm", "0012_lead_lead_source"),
        ("organizations", "0004_organizationpayment"),
    ]

    operations = [
        migrations.CreateModel(
            name="SmartTrigger",
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
                ("name", models.CharField(max_length=255)),
                ("description", models.CharField(blank=True, max_length=300)),
                (
                    "event_type",
                    models.CharField(
                        choices=[
                            ("lead.created", "Lead created"),
                            ("lead.updated", "Lead updated"),
                            ("lead.stage_changed", "Lead stage changed"),
                            ("whatsapp.received", "WhatsApp message received"),
                        ],
                        max_length=32,
                    ),
                ),
                (
                    "condition_mode",
                    models.CharField(
                        choices=[
                            ("all", "Match all conditions"),
                            ("any", "Match any condition"),
                        ],
                        default="all",
                        max_length=8,
                    ),
                ),
                ("conditions", models.JSONField(blank=True, default=list)),
                ("actions", models.JSONField(default=list)),
                ("is_active", models.BooleanField(default=True)),
                ("once_per_lead", models.BooleanField(default=False)),
                ("cooldown_minutes", models.PositiveIntegerField(default=0)),
                ("successful_runs", models.PositiveIntegerField(default=0)),
                ("failed_runs", models.PositiveIntegerField(default=0)),
                ("last_fired_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_smart_triggers",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="smart_triggers",
                        to="organizations.organization",
                    ),
                ),
            ],
            options={"ordering": ["-updated_at"]},
        ),
        migrations.CreateModel(
            name="TriggerExecution",
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
                ("event_id", models.UUIDField()),
                ("event_type", models.CharField(max_length=32)),
                ("event_payload", models.JSONField(blank=True, default=dict)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("processing", "Processing"),
                            ("success", "Success"),
                            ("skipped", "Skipped"),
                            ("failed", "Failed"),
                        ],
                        max_length=12,
                    ),
                ),
                ("matched", models.BooleanField(default=False)),
                ("skip_reason", models.CharField(blank=True, max_length=255)),
                ("action_results", models.JSONField(blank=True, default=list)),
                ("error", models.TextField(blank=True)),
                ("started_at", models.DateTimeField()),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "lead",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="trigger_executions",
                        to="crm.lead",
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="trigger_executions",
                        to="organizations.organization",
                    ),
                ),
                (
                    "trigger",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="executions",
                        to="triggers.smarttrigger",
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddConstraint(
            model_name="smarttrigger",
            constraint=models.UniqueConstraint(
                fields=("organization", "name"),
                name="trigger_org_name_uniq",
            ),
        ),
        migrations.AddIndex(
            model_name="smarttrigger",
            index=models.Index(
                fields=["organization", "is_active", "event_type"],
                name="trigger_org_event_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="smarttrigger",
            index=models.Index(
                fields=["organization", "updated_at"],
                name="trigger_org_updated_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="triggerexecution",
            constraint=models.UniqueConstraint(
                fields=("trigger", "event_id"),
                name="trigger_event_once_uniq",
            ),
        ),
        migrations.AddIndex(
            model_name="triggerexecution",
            index=models.Index(
                fields=["organization", "status", "created_at"],
                name="trigger_exec_org_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="triggerexecution",
            index=models.Index(
                fields=["trigger", "created_at"],
                name="trigger_exec_rule_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="triggerexecution",
            index=models.Index(
                fields=["lead", "trigger", "created_at"],
                name="trigger_exec_lead_idx",
            ),
        ),
    ]
