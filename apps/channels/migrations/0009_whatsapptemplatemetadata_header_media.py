from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("channels", "0008_whatsapptemplatemetadata_whatsapptemplateoperation"),
    ]

    operations = [
        migrations.AddField(
            model_name="whatsapptemplatemetadata",
            name="header_file_name",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="whatsapptemplatemetadata",
            name="header_file_size",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="whatsapptemplatemetadata",
            name="header_mime_type",
            field=models.CharField(blank=True, max_length=128),
        ),
        migrations.AddField(
            model_name="whatsapptemplatemetadata",
            name="header_sample_handle",
            field=models.TextField(blank=True),
        ),
    ]
