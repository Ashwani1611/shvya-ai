import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0010_merge_operations_audit_branches"),
    ]

    operations = [
        migrations.AlterField(
            model_name="diagnosticaccesslog",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="diagnostic_access_logs",
                to="organizations.organization",
            ),
        ),
        migrations.AlterField(
            model_name="diagnosticaccesslog",
            name="api_key",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="diagnostic_access_logs",
                to="organizations.apikey",
            ),
        ),
    ]
