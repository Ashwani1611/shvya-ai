import django.db.models.deletion
from django.db import migrations, models
import uuid


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0006_operations_mcp"),
    ]

    operations = [
        migrations.CreateModel(
            name="OperationsApprovalUse",
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
                    "created_at",
                    models.DateTimeField(auto_now_add=True),
                ),
                (
                    "approval_event",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="approval_use",
                        to="integrations.operationsauditevent",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
    ]
