from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("channels", "0009_whatsapptemplatemetadata_header_media"),
    ]

    operations = [
        migrations.AddField(
            model_name="whatsapptemplatemetadata",
            name="carousel_config",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
