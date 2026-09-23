from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("triggers", "0002_triggerevent_st_pending_event_idx_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="smarttrigger",
            name="is_active",
            field=models.BooleanField(db_index=True, default=True),
        ),
    ]
