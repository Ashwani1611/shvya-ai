from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("shvya_calendar", "0005_repair_default_booked_at")]
    operations = [migrations.AddField(
        model_name="calendarbooking", name="google_checked_at",
        field=models.DateTimeField(blank=True, db_index=True, null=True),
    )]
