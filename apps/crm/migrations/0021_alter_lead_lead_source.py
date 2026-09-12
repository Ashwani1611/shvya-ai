from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("crm", "0020_pipeline_ai_enabled")]

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
                ],
                default="system",
                max_length=30,
            ),
        ),
    ]
