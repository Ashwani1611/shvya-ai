from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("followups", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="followupstep",
            name="recurring_weekdays",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
