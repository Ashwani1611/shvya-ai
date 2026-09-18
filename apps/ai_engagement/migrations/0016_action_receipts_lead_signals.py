import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("ai_engagement", "0015_aitrace")]

    operations = [
        migrations.CreateModel(
            name="AIActionReceipt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source_message_id", models.UUIDField()),
                ("idempotency_key", models.CharField(max_length=64)),
                ("action_type", models.CharField(max_length=40)),
                ("result", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="organizations.organization")),
                ("lead", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="crm.lead")),
            ],
            options={
                "indexes": [models.Index(fields=["organization", "lead", "source_message_id"], name="ai_receipt_org_lead_source")],
                "constraints": [models.UniqueConstraint(fields=("organization", "idempotency_key"), name="ai_receipt_org_key_unique")],
            },
        ),
        migrations.CreateModel(
            name="LeadSignal",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source_message_id", models.UUIDField()),
                ("signal", models.CharField(max_length=64)),
                ("value", models.JSONField(blank=True, default=dict)),
                ("confidence", models.FloatField(default=1.0)),
                ("evidence", models.CharField(blank=True, max_length=500)),
                ("observed_at", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="organizations.organization")),
                ("lead", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="crm.lead")),
            ],
            options={
                "indexes": [
                    models.Index(fields=["organization", "lead", "-observed_at"], name="ai_signal_org_lead_time"),
                    models.Index(fields=["organization", "signal", "-observed_at"], name="ai_signal_org_kind_time"),
                ],
                "constraints": [models.UniqueConstraint(fields=("organization", "lead", "source_message_id", "signal"), name="ai_signal_source_kind_unique")],
            },
        ),
    ]
