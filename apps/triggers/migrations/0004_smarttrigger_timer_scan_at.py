from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("triggers", "0003_smarttrigger_is_active"),
    ]

    operations = [
        migrations.AddField(
            model_name="smarttrigger",
            name="timer_scan_at",
            field=models.DateTimeField(
                blank=True,
                db_index=True,
                null=True,
            ),
        ),
    ]
