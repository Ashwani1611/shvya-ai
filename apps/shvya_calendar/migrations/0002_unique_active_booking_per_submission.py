from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("shvya_calendar", "0001_initial"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="calendarbooking",
            constraint=models.UniqueConstraint(
                condition=models.Q(status__in=["scheduled", "rescheduled"]),
                fields=("submission",),
                name="uniq_active_calendar_booking_submission",
            ),
        ),
    ]
