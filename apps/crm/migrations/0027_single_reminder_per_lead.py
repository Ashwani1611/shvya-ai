from django.db import migrations, models
from django.db.models import Count


def keep_newest_reminder_per_lead(apps, schema_editor):
    LeadReminder = apps.get_model("crm", "LeadReminder")
    duplicates = (
        LeadReminder.objects
        .values("lead_id")
        .annotate(total=Count("id"))
        .filter(total__gt=1)
    )
    for row in duplicates.iterator():
        reminders = LeadReminder.objects.filter(
            lead_id=row["lead_id"]
        ).order_by("-created_at", "-updated_at", "-due_at")
        newest = reminders.first()
        if newest is not None:
            reminders.exclude(pk=newest.pk).delete()


class Migration(migrations.Migration):
    # PostgreSQL cannot ALTER a table while row deletes in the same transaction
    # still have pending FK trigger events. Commit duplicate cleanup first.
    atomic = False

    dependencies = [
        ("crm", "0026_alter_lead_lead_source"),
    ]

    operations = [
        migrations.RunPython(
            keep_newest_reminder_per_lead,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="leadreminder",
            constraint=models.UniqueConstraint(
                fields=("lead",),
                name="uniq_lead_reminder_per_lead",
            ),
        ),
    ]
