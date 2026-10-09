"""99acres connector, successful enquiry receipts, and privacy-safe event logs."""
import uuid

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("crm", "0036_alter_lead_lead_source_99acres"),
        ("integrations", "0021_operationsaiflowrun_operationsaiflowreservation_and_more"),
        ("organizations", "0007_organizationdeletioncleanup"),
    ]

    operations = [
        migrations.CreateModel(
            name="Acres99Integration",
            fields=[
                ("id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ("mode", models.CharField(
                    max_length=8, default="push",
                    choices=[("push", "Push (webhook)"), ("pull", "Pull (scheduled)"), ("both", "Push + Pull")],
                )),
                ("is_enabled", models.BooleanField(default=False)),
                ("webhook_token", models.UUIDField(null=True, blank=True, unique=True, editable=False)),
                ("encrypted_username", models.TextField(blank=True)),
                ("encrypted_password", models.TextField(blank=True)),
                ("encrypted_api_token", models.TextField(blank=True)),
                ("requested_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("provisioned_at", models.DateTimeField(null=True, blank=True)),
                ("last_received_at", models.DateTimeField(null=True, blank=True)),
                ("last_synced_at", models.DateTimeField(null=True, blank=True)),
                ("sync_cursor", models.DateTimeField(null=True, blank=True)),
                ("last_poll_at", models.DateTimeField(null=True, blank=True)),
                ("poll_hour_start", models.DateTimeField(null=True, blank=True)),
                ("poll_hour_count", models.PositiveSmallIntegerField(default=0)),
                ("last_error", models.CharField(max_length=500, blank=True)),
                ("received_count", models.PositiveBigIntegerField(default=0)),
                ("created_count", models.PositiveBigIntegerField(default=0)),
                ("linked_count", models.PositiveBigIntegerField(default=0)),
                ("failed_count", models.PositiveBigIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("organization", models.OneToOneField(
                    to="organizations.organization", related_name="acres99_integration",
                    on_delete=django.db.models.deletion.CASCADE,
                )),
                ("pipeline", models.ForeignKey(
                    to="crm.pipeline", related_name="acres99_integrations",
                    on_delete=django.db.models.deletion.RESTRICT, null=True, blank=True,
                )),
                ("stage", models.ForeignKey(
                    to="crm.stage", related_name="acres99_integrations",
                    on_delete=django.db.models.deletion.RESTRICT, null=True, blank=True,
                )),
            ],
            options={"ordering": ["organization__name"], "verbose_name": "99acres Integration"},
        ),
        migrations.CreateModel(
            name="Acres99Receipt",
            fields=[
                ("id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ("external_query_id", models.CharField(max_length=180)),
                ("direction", models.CharField(max_length=4, choices=[("push", "Push"), ("pull", "Pull")])),
                ("property_id", models.CharField(max_length=120, blank=True)),
                ("received_at", models.DateTimeField(auto_now_add=True)),
                ("integration", models.ForeignKey(
                    to="integrations.acres99integration", related_name="receipts",
                    on_delete=django.db.models.deletion.CASCADE,
                )),
                ("lead", models.ForeignKey(
                    to="crm.lead", null=True, blank=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                )),
            ],
            options={
                "ordering": ["-received_at"],
                "constraints": [models.UniqueConstraint(
                    fields=("integration", "external_query_id"),
                    name="uniq_acres99_external_query",
                )],
                "indexes": [models.Index(
                    fields=["integration", "received_at"], name="acres99_receipt_time",
                )],
            },
        ),
        migrations.CreateModel(
            name="Acres99Event",
            fields=[
                ("id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ("external_query_id", models.CharField(max_length=180, blank=True)),
                ("direction", models.CharField(max_length=4, choices=[("push", "Push"), ("pull", "Pull")])),
                ("status", models.CharField(max_length=12, choices=[
                    ("created", "Created"), ("linked", "Existing contact"),
                    ("duplicate", "Duplicate"), ("failed", "Failed"),
                ])),
                ("error_code", models.CharField(max_length=100, blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("integration", models.ForeignKey(
                    to="integrations.acres99integration", related_name="events",
                    on_delete=django.db.models.deletion.CASCADE,
                )),
                ("lead", models.ForeignKey(
                    to="crm.lead", null=True, blank=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                )),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [models.Index(
                    fields=["integration", "created_at"], name="acres99_event_time",
                )],
            },
        ),
    ]
