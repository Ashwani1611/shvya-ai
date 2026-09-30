from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("channels", "0017_ai_send_state_and_message_sent_at")]

    operations = [
        migrations.AddField(
            model_name="instagramwebhookdelivery",
            name="dispatched_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
