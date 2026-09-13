from django.db import migrations


def repair_pipeline_stage_consistency(apps, schema_editor):
    Lead = apps.get_model("crm", "Lead")
    Stage = apps.get_model("crm", "Stage")

    leads = Lead.objects.select_related("stage", "pipeline").all().iterator(chunk_size=500)
    for lead in leads:
        if not lead.stage_id or not lead.pipeline_id:
            continue
        if lead.stage.pipeline_id == lead.pipeline_id:
            continue

        # Pipeline is the persisted CRM location shown by the dashboard and is
        # therefore authoritative. Old bulk API code could update stage_id alone;
        # repair that corruption by selecting the same-named stage in the current
        # pipeline, falling back to its first active stage only when necessary.
        target = (
            Stage.objects.filter(
                pipeline_id=lead.pipeline_id,
                name__iexact=lead.stage.name,
                is_active=True,
            )
            .order_by("display_order", "id")
            .first()
        )
        if target is None:
            target = (
                Stage.objects.filter(
                    pipeline_id=lead.pipeline_id,
                    is_active=True,
                )
                .order_by("display_order", "id")
                .first()
            )
        if target is not None:
            Lead.objects.filter(pk=lead.pk).update(stage_id=target.pk)


class Migration(migrations.Migration):
    dependencies = [
        ("crm", "0023_alter_lead_lead_source"),
    ]

    operations = [
        migrations.RunPython(
            repair_pipeline_stage_consistency,
            migrations.RunPython.noop,
        ),
    ]
