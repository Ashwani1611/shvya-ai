import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("ai_engagement", "0021_orginfo_model_routing")]

    operations = [
        migrations.AlterField(
            model_name="aitrace", name="lead",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                                    related_name="ai_traces", to="crm.lead"),
        ),
        migrations.AlterField(
            model_name="aitrace", name="connection_type",
            field=models.CharField(max_length=16, choices=[("api", "API"), ("hosted", "Hosted"),
                                                         ("instagram", "Instagram"), ("sandbox", "Sandbox")]),
        ),
        migrations.AddConstraint(
            model_name="aitrace",
            constraint=models.CheckConstraint(
                condition=(models.Q(connection_type="sandbox", lead__isnull=True)
                           | (~models.Q(connection_type="sandbox") & models.Q(lead__isnull=False))),
                name="ai_trace_sandbox_lead_scope",
            ),
        ),
    ]
