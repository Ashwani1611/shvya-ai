from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("channels", "0018_instagram_webhook_dispatch_lease")]
    operations = [
        migrations.AddField(
            model_name="whatsapptemplatemetadata",
            name="delivery_media",
            field=models.JSONField(default=dict, blank=True),
        ),
        migrations.AddField(
            model_name="whatsapptemplatemetadata",
            name="delivery_bindings",
            field=models.JSONField(default=dict, blank=True),
        ),
    ]
