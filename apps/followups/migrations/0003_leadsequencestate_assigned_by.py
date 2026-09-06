import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("followups", "0002_followupstep_recurring_weekdays"),
    ]

    operations = [
        migrations.AddField(
            model_name="leadsequencestate",
            name="assigned_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="assigned_followup_sequence_states",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
