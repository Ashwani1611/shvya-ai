from django.db import migrations, models


def preserve_disabled_leads(apps, schema_editor):
    states = apps.get_model("followups", "LeadSequenceState")
    leads = apps.get_model("crm", "Lead")
    leads.objects.filter(pk__in=states.objects.filter(
        status__in=["active", "paused"], lead_auto_followup_enabled=False,
    ).values("lead_id")).update(auto_followup_enabled=False)


class Migration(migrations.Migration):
    dependencies = [("crm", "0027_single_reminder_per_lead"), ("followups", "0005_touchpointcategory_touchpointreply_and_more")]
    operations = [
        migrations.AddField(model_name="lead", name="auto_followup_enabled", field=models.BooleanField(default=True)),
        migrations.RunPython(preserve_disabled_leads, migrations.RunPython.noop),
    ]
