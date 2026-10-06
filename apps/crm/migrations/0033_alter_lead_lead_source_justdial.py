from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("crm", "0032_instagram_phone_optional"),
    ]

    operations = [
        migrations.AlterField(
            model_name="lead",
            name="lead_source",
            field=models.CharField(
                choices=[
                    ("system", "System"),
                    ("external_api", "External API"),
                    ("whatsapp_api", "WhatsApp API"),
                    ("whatsapp", "WhatsApp"),
                    ("google_sheets", "Google Sheet"),
                    ("csv_import", "CSV Import"),
                    ("meta_ads", "Meta Ads"),
                    ("instagram", "Instagram"),
                    ("shvya_calendar", "SHVYA Calendar"),
                    ("phone_call", "Phone Call"),
                    ("justdial", "JustDial"),
                ],
                default="system",
                max_length=30,
            ),
        ),
    ]
