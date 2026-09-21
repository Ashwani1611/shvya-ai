from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("channels", "0015_instagramconversation_lead"),
    ]

    operations = [
        migrations.AddField(
            model_name="instagramwebhookdelivery",
            name="organization_ids",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="instagramwebhookdelivery",
            name="account_ids",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
