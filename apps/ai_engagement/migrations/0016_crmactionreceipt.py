import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ai_engagement", "0015_aitrace"),
    ]

    operations = [
        migrations.CreateModel(
            name="CRMActionReceipt",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("source_inbound_message_id", models.UUIDField()),
                ("idempotency_key", models.CharField(max_length=64)),
                ("action_type", models.CharField(max_length=32)),
                ("result", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("lead", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="ai_action_receipts", to="crm.lead")),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="ai_action_receipts", to="organizations.organization")),
            ],
            options={
                "constraints": [models.UniqueConstraint(
                    fields=("organization", "lead", "source_inbound_message_id", "idempotency_key"),
                    name="ai_action_receipt_unique",
                )],
            },
        ),
    ]
