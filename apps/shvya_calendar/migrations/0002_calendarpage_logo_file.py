import apps.shvya_calendar.models
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("shvya_calendar", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="calendarpage",
            name="logo_file",
            field=models.FileField(
                blank=True,
                max_length=300,
                upload_to=apps.shvya_calendar.models.calendar_logo_path,
            ),
        ),
    ]
