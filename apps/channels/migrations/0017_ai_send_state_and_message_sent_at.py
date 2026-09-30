import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("channels", "0016_hosted_gateway_sharding"),
    ]

    operations = [
        migrations.AddField(
            model_name="whatsappmessage",
            name="sent_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.CreateModel(
            name="AIMessageSendState",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("next_send_at", models.DateTimeField(blank=True, null=True)),
                ("last_sent_at", models.DateTimeField(blank=True, null=True)),
                ("claim_token", models.UUIDField(blank=True, null=True)),
                ("claimed_until", models.DateTimeField(blank=True, null=True)),
                ("account", models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="ai_send_state", to="channels.whatsappaccount",
                )),
            ],
        ),
    ]
