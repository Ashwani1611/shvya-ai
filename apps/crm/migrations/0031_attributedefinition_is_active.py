from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("crm", "0030_alter_lead_lead_source_call"),
    ]

    operations = [
        migrations.AddField(
            model_name="attributedefinition",
            name="is_active",
            field=models.BooleanField(
                db_index=True,
                default=True,
                help_text=(
                    "Archived attributes are retained for history but excluded "
                    "from active configuration."
                ),
            ),
        ),
    ]
