from django.db import migrations


def backfill_google_sheet_activity_source(apps, schema_editor):
    LeadActivity = apps.get_model("crm", "LeadActivity")

    LeadActivity.objects.filter(
        topic="lead_created",
        actor__isnull=True,
        actor_name="",
        lead__lead_source="google_sheets",
    ).update(actor_name="Google Sheet")


class Migration(migrations.Migration):
    dependencies = [
        ("crm", "0021_alter_lead_lead_source"),
    ]

    operations = [
        migrations.RunPython(
            backfill_google_sheet_activity_source,
            migrations.RunPython.noop,
        ),
    ]
