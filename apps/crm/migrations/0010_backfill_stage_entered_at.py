from django.db import migrations


def backfill_stage_entered_at(
    apps,
    schema_editor,
):
    Lead = apps.get_model(
        "crm",
        "Lead",
    )

    LeadActivity = apps.get_model(
        "crm",
        "LeadActivity",
    )

    for lead in Lead.objects.all().iterator():

        latest_stage_activity = (
            LeadActivity.objects
            .filter(
                lead_id=lead.id,
                topic="stage_changed",
                new_stage_id=lead.stage_id,
            )
            .order_by(
                "-created_at",
            )
            .first()
        )

        if latest_stage_activity:

            lead.stage_entered_at = (
                latest_stage_activity.created_at
            )

        else:

            lead.stage_entered_at = (
                lead.updated_at
            )

        lead.save(
            update_fields=[
                "stage_entered_at",
            ]
        )


class Migration(
    migrations.Migration,
):

    dependencies = [
        (
            "crm",
            "0009_lead_stage_entered_at",
        ),
    ]

    operations = [
        migrations.RunPython(
            backfill_stage_entered_at,
            migrations.RunPython.noop,
        ),
    ]