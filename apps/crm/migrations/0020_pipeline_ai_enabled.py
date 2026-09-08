from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("crm", "0019_alter_lead_lead_source")]

    operations = [
        migrations.AddField(
            model_name="pipeline",
            name="ai_enabled",
            field=models.BooleanField(
                default=True,
                help_text="Allow SHVYA AI to engage leads in this pipeline.",
            ),
        ),
    ]
