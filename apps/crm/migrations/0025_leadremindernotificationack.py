# Generated manually for persistent reminder notification acknowledgements.
import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0024_repair_lead_pipeline_stage_consistency"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="LeadReminderNotificationAck",
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
                ("acknowledged_at", models.DateTimeField(auto_now_add=True)),
                (
                    "reminder",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="notification_acknowledgements",
                        to="crm.leadreminder",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="reminder_notification_acknowledgements",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["user", "acknowledged_at"],
                        name="crm_rem_ack_user_at_idx",
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("reminder", "user"),
                        name="uniq_reminder_notification_ack_user",
                    )
                ],
            },
        ),
    ]
