import apps.followups.touchpoint_models
import apps.support.storage
import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("followups", "0009_followupsequence_instagram_account_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="TouchpointAttachment",
            fields=[
                ("id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ("file", models.FileField(
                    storage=apps.support.storage.private_storage,
                    upload_to=apps.followups.touchpoint_models.touchpoint_attachment_upload_to,
                    max_length=300,
                )),
                ("original_name", models.CharField(max_length=255)),
                ("mime_type", models.CharField(max_length=120)),
                ("size", models.PositiveBigIntegerField()),
                ("position", models.PositiveSmallIntegerField(default=1)),
                ("reply", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="attachments", to="followups.touchpointreply",
                )),
            ],
            options={"ordering": ["position", "id"]},
        ),
        migrations.AddConstraint(
            model_name="touchpointattachment",
            constraint=models.UniqueConstraint(
                fields=("reply", "position"),
                name="touchpoint_attachment_position_uniq",
            ),
        ),
    ]
