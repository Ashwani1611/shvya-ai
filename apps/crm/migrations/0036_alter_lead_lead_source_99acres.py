from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("crm", "0035_lead_is_operations_test")]

    operations = [
        migrations.AlterField(
            model_name="lead",
            name="lead_source",
            field=models.CharField(
                max_length=30,
                default="system",
                choices=[
                    ("system", "System"),
                    ("external_api", "External API"),
                    ("whatsapp_api", "WhatsApp API"),
                    ("whatsapp", "WhatsApp"),
                    ("google_sheets", "Google Sheet"),
                    ("indiamart", "IndiaMART"),
                    ("csv_import", "CSV Import"),
                    ("meta_ads", "Meta Ads"),
                    ("instagram", "Instagram"),
                    ("shvya_calendar", "SHVYA Calendar"),
                    ("phone_call", "Phone Call"),
                    ("justdial", "JustDial"),
                    ("99acres", "99acres"),
                ],
            ),
        ),
    ]
