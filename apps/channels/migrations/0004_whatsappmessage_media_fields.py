from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("channels", "0003_add_missing_is_read_column"),
    ]

    operations = [
        migrations.AddField(
            model_name="whatsappmessage",
            name="message_type",
            field=models.CharField(
                choices=[
                    ("text", "Text"),
                    ("image", "Image"),
                    ("audio", "Audio"),
                    ("video", "Video"),
                    ("document", "Document"),
                ],
                default="text",
                help_text="WhatsApp message transport type.",
                max_length=15,
            ),
        ),
        migrations.AddField(
            model_name="whatsappmessage",
            name="media_payload",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text=(
                    "Controlled media metadata used when message_type "
                    "is image, audio, video, or document."
                ),
            ),
        ),
        migrations.AddIndex(
            model_name="whatsappmessage",
            index=models.Index(
                fields=[
                    "lead",
                    "message_type",
                    "created_at",
                ],
                name="wa_msg_lead_type_created_idx",
            ),
        ),
    ]