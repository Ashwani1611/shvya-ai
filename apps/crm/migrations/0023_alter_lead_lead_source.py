from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("crm", "0022_backfill_google_sheet_activity_source")]

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
                    ("meta_ads", "Meta ads"),
                ],
                default="system",
                max_length=30,
            ),
        ),
    ]
