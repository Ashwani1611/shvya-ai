from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("triggers", "0004_smarttrigger_timer_scan_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="smarttrigger",
            name="timer_lead_cursor",
            field=models.UUIDField(
                blank=True,
                null=True,
            ),
        ),
    ]
