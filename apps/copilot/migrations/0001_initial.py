import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("organizations", "0004_organizationpayment"),
        ("crm", "0012_lead_lead_source"),
    ]

    operations = [
        migrations.CreateModel(
            name="CopilotScanState",
            fields=[
                (
                    "organization",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        related_name="copilot_scan_state",
                        serialize=False,
                        to="organizations.organization",
                    ),
                ),
                ("last_refreshed_at", models.DateTimeField(blank=True, null=True)),
                ("last_error", models.TextField(blank=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name="CopilotLeadFlag",
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
                (
                    "flag_code",
                    models.CharField(
                        choices=[
                            ("R1", "Reply Pending"),
                            ("R2", "New Lead, No Contact"),
                            ("R3", "Delivery Failure / Delay"),
                            ("C1", "No Calls Ever Made"),
                            ("C2", "Call Gap"),
                            ("C3", "All Calls No Response"),
                            ("H1", "High Intent, No Action"),
                            ("H2", "Was Engaging, Now Silent"),
                            ("H3", "No Automation Running"),
                            ("S1", "Follow-ups Exhausted, Lead Silent"),
                            ("S2", "Sequence Complete, No Stage Move"),
                            ("X2", "No Phone Number"),
                            ("X3", "Stage Stale"),
                            ("X4", "Long-Term Dormant"),
                        ],
                        max_length=2,
                    ),
                ),
                (
                    "severity",
                    models.CharField(
                        choices=[
                            ("low", "Low"),
                            ("medium", "Medium"),
                            ("high", "High"),
                            ("critical", "Critical"),
                        ],
                        max_length=10,
                    ),
                ),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("first_detected_at", models.DateTimeField(auto_now_add=True)),
                ("last_detected_at", models.DateTimeField(auto_now=True)),
                ("snoozed_until", models.DateTimeField(blank=True, null=True)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "lead",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="copilot_flags",
                        to="crm.lead",
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="copilot_flags",
                        to="organizations.organization",
                    ),
                ),
            ],
            options={"ordering": ["-last_detected_at"]},
        ),
        migrations.AddConstraint(
            model_name="copilotleadflag",
            constraint=models.UniqueConstraint(
                fields=("organization", "lead", "flag_code"),
                name="uniq_copilot_org_lead_flag",
            ),
        ),
        migrations.AddIndex(
            model_name="copilotleadflag",
            index=models.Index(
                fields=["organization", "severity"],
                name="copilot_org_severity_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="copilotleadflag",
            index=models.Index(
                fields=["organization", "flag_code"],
                name="copilot_org_code_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="copilotleadflag",
            index=models.Index(
                fields=["organization", "snoozed_until"],
                name="copilot_org_snooze_idx",
            ),
        ),
    ]
